#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <aclapi.h>
#include <userenv.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <wchar.h>
#include <errno.h>

/* V5 diagnostic only. No ACL/label/token mutation, impersonation, privilege,
 * arbitrary registry path, inherited HKEY, enumeration or process creation.
 * Every actual Win32 call below is counted, including SD length/free, close
 * and two IPC writes. Pseudo accessors, LastError and CRT bookkeeping alone
 * are excluded. Separate V5 limits; V4 and production limits are unchanged.
 * A missing frame or any unknown is never resistance evidence.
 * argv: IPC, seed-low|deny-untrusted, seed_<nonce>, disposable_<nonce>,
 * attempt_<nonce>, 16 lowercase hex digits. Nonce is 32 lowercase hex digits;
 * the host must additionally bind it to its newly created profile receipt.
 * Normal complete paths: Low 74 calls; Untrusted 64 calls, including IPC.
 * Each API group reserves its worst evidence, future owned closes and IPC.
 * Failure/partial-output paths stop probes and consume only reserved closes.
 */
#define CALL_BUDGET 80
#define FRAME_BUDGET 8192
#define NODE_BUDGET 1024
#define SD_BUFFER 65536
#define ACL_BYTES 512
#define LABEL_BYTES 64
#define ACE_LIMIT 8
#define FINAL_BYTES 768
#define FINAL_NODES 64
enum { TOKEN_QUERY_CALL=1, THREAD_OPEN, ROOT_OPEN, SECURITY_QUERY, SD_LENGTH,
    SD_FREE, KEY_CREATE, VALUE_SET, VALUE_QUERY, KEY_OPEN, KEY_DELETE, KEY_CLOSE,
    THREAD_CLOSE, IPC_WRITE };
enum { P_NONE, P_BUDGET, P_TOKEN, P_THREAD, P_POINTER, P_ROOT, P_SD, P_IDENTITY,
    P_SD_CHANGED, P_COLLISION, P_REGISTRY, P_CANARY, P_CLOSE, P_HANDLE, P_FRAME };
typedef struct { unsigned api, detail, state; int64_t raw; DWORD error, length; } CALL;
typedef struct {
    BOOL valid, thread_absent, il_known, policy_known;
    DWORD type, ac, cap, il, il_attrs, policy, user_attrs;
    LUID id;
    char user[192], package[192];
} TOKEN_SNAPSHOT;
typedef struct {
    BOOL valid, dacl_complete, label_complete;
    unsigned reason;
    DWORD bytes, control, dacl_bytes, dacl_count, dacl_revision, label_bytes,
        label_count, il, label_mask, label_flags, package_count, package_flags, package_mask;
    char owner[192]; BYTE dacl[ACL_BYTES], label[LABEL_BYTES];
} SD_SNAPSHOT;
typedef struct { HKEY value; unsigned role, generation; BOOL attempted; } KEY_OWNER;
/* Gate rows are step, first-call inclusive, last-call exclusive, token/root/
 * child snapshot indexes. References deduplicate equal values, never assert
 * same-object identity. Every gate still makes fresh native observations.
 */
typedef struct { unsigned step, first_call, last_call; int token, root, child; } GATE;
typedef struct { unsigned call_index; DWORD type, bytes; BYTE buffer[8]; } CANARY_READ;
typedef char require_x64[(sizeof(void *)==8 && sizeof(TOKEN_STATISTICS)==56) ? 1 : -1];
__declspec(align(8)) static BYTE token_buffer[SD_BUFFER], sd_buffer[SD_BUFFER];
static CALL calls[CALL_BUDGET];
static TOKEN_SNAPSHOT tokens[2];
static SD_SNAPSHOT security[4];
static GATE gates[4];
static CANARY_READ canary_reads[3];
static KEY_OWNER root_key={0,1,0,0}, seed_key={0,2,0,0}, disposable_key={0,3,0,0}, write_key={0,4,0,0};
static HANDLE thread_partial;
static PSECURITY_DESCRIPTOR unreleased_descriptor;
static BOOL thread_close_attempted, release_failed, boundary_failed, complete, frame_ok;
static unsigned call_count, token_count, security_count, gate_count, canary_read_count, problem_code, refused_handles, mode;
static DWORD received;
static int root_reference=-1, seed_reference=-1, disposable_reference=-1;
static wchar_t *seed_name, *disposable_name, *attempt_name;
static BYTE canary[8];
static char canary_hex[17], nonce_hex[33], frame[FRAME_BUDGET];
static size_t used;

static void problem(unsigned code) { if (!problem_code) problem_code=code; }
static DWORD u32(const BYTE *p) { DWORD v; memcpy(&v,p,4); return v; }
static WORD u16(const BYTE *p) { WORD v; memcpy(&v,p,2); return v; }
static BOOL real_handle(HANDLE h) {
    uintptr_t v=(uintptr_t)h;
    return v && v<(uintptr_t)-6 && !(v>=0x80000000ULL && v<=0x80000060ULL)
        && !(v>=0xffffffff80000000ULL && v<=0xffffffff80000060ULL);
}
static unsigned pending_closes(void) {
    KEY_OWNER *owners[]={&root_key,&seed_key,&disposable_key,&write_key}; unsigned i,n=0;
    for(i=0;i<4;i++) if(owners[i]->value && !owners[i]->attempted) ++n;
    return n+(thread_partial && !thread_close_attempted ? 1u:0u);
}
static CALL *entered(unsigned api,unsigned detail) {
    CALL *c;
    if(call_count>=CALL_BUDGET) { problem(P_BUDGET); return NULL; }
    c=&calls[call_count++]; memset(c,0,sizeof(*c)); c->api=api; c->detail=detail; c->state=1; return c;
}
static void returned(CALL *c,int64_t raw,DWORD error,DWORD length) {
    c->raw=raw; c->error=error; c->length=length; c->state=2;
}
static void append(const char *format,...) {
    va_list args; int n;
    if(!frame_ok) return;
    va_start(args,format); n=vsnprintf(frame+used,sizeof(frame)-used,format,args); va_end(args);
    if(n<0 || (size_t)n>=sizeof(frame)-used) { frame_ok=FALSE; return; }
    used+=(size_t)n;
}
static void hex(const BYTE *data,size_t n) { size_t i; for(i=0;i<n;i++) append("%02x",(unsigned)data[i]); }
static void token_json(const TOKEN_SNAPSHOT *s) {
    append("{\"valid\":%d,\"thread_absent\":%d,\"type\":%lu,\"user\":\"%s\",\"package\":\"%s\","
        "\"id\":[%lu,%ld],\"ac\":%lu,\"cap\":%lu,\"il_known\":%d,\"il\":%lu,\"il_attrs\":%lu,"
        "\"policy_known\":%d,\"policy\":%lu,\"user_attrs\":%lu}",s->valid,s->thread_absent,s->type,
        s->user,s->package,s->id.LowPart,s->id.HighPart,s->ac,s->cap,s->il_known,s->il,s->il_attrs,
        s->policy_known,s->policy,s->user_attrs);
}
static void sd_json(const SD_SNAPSHOT *s) {
    append("{\"valid\":%d,\"reason\":%u,\"sd_bytes\":%lu,\"control\":%lu,\"owner\":\"%s\","
        "\"dacl_bytes\":%lu,\"dacl_count\":%lu,\"acl_revision\":%lu,\"dacl_complete\":%d,\"dacl_hex\":\"",
        s->valid,s->reason,s->bytes,s->control,s->owner,s->dacl_bytes,s->dacl_count,s->dacl_revision,s->dacl_complete);
    if(s->dacl_complete) hex(s->dacl,s->dacl_bytes);
    append("\",\"label_bytes\":%lu,\"label_count\":%lu,\"label_complete\":%d,\"label_hex\":\"",
        s->label_bytes,s->label_count,s->label_complete);
    if(s->label_complete) hex(s->label,s->label_bytes);
    append("\",\"il\":%lu,\"label_mask\":%lu,\"label_flags\":%lu,\"package_count\":%lu,"
        "\"package_flags\":%lu,\"package_mask\":%lu}",s->il,s->label_mask,s->label_flags,
        s->package_count,s->package_flags,s->package_mask);
}
static unsigned node_bound(void) {
    /* Conservative count for this fixed schema, including JSON object keys. */
    return 128+token_count*36+security_count*46+gate_count*8+canary_read_count*6+call_count*7;
}
static void render(void) {
    unsigned i,block; BOOL cleanup_ok=!pending_closes() && !root_key.value && !seed_key.value
        && !disposable_key.value && !write_key.value && !thread_partial && !unreleased_descriptor && !release_failed && !refused_handles;
    frame_ok=TRUE; used=0;
    append("{\"probe\":\"registry_integrity_v5\",\"mode\":\"%s\",\"boundary_failed\":%d,"
        "\"positive_observed\":%d,\"resistance_observed\":%d,\"unknown\":%d,\"problem\":%u,"
        "\"cleanup_ok\":%d,\"refused_handles\":%u,\"release_failed\":%d,\"canary_hex\":\"%s\","
        "\"nonce\":\"%s\",\"token_snapshots\":[",mode==1?"seed-low":"deny-untrusted",boundary_failed,
        mode==1 && complete && !problem_code && cleanup_ok,mode==2 && complete && !boundary_failed && !problem_code && cleanup_ok,
        !boundary_failed && (!complete || problem_code || !cleanup_ok),problem_code,cleanup_ok,refused_handles,release_failed,canary_hex,nonce_hex);
    for(i=0;i<token_count;i++) { if(i) append(","); token_json(&tokens[i]); }
    append("],\"security_snapshots\":[");
    for(i=0;i<security_count;i++) { if(i) append(","); sd_json(&security[i]); }
    append("],\"gates\":[");
    for(i=0;i<gate_count;i++) { GATE *g=&gates[i]; append("%s[%u,%u,%u,%d,%d,%d]",i?",":"",g->step,g->first_call,g->last_call,g->token,g->root,g->child); }
    append("],\"key_owners\":[");
    { KEY_OWNER *owners[]={&root_key,&seed_key,&disposable_key,&write_key};
      for(i=0;i<4;i++) append("%s[%u,%u,\"%016llx\",%d]",i?",":"",owners[i]->role,owners[i]->generation,
          (unsigned long long)(uintptr_t)owners[i]->value,owners[i]->attempted); }
    append("],\"thread_partial_hex\":\"%016llx\",\"thread_close_attempted\":%d,\"unreleased_descriptor_hex\":\"%016llx\",\"canary_reads\":[",
        (unsigned long long)(uintptr_t)thread_partial,thread_close_attempted,(unsigned long long)(uintptr_t)unreleased_descriptor);
    for(i=0;i<canary_read_count;i++) { CANARY_READ *r=&canary_reads[i];
        append("%s[%u,%lu,%lu,\"",i?",":"",r->call_index,r->type,r->bytes); hex(r->buffer,8); append("\"]"); }
    append("],\"canary_read_encoding\":\"call_index,type,returned_bytes,fixed8buffer_hex_not_complete_on_failure\","
        "\"call_budget\":80,\"call_count_before_ipc\":%u,\"maximum_total_calls\":%u,\"call_blocks\":[",call_count,call_count+2);
    for(block=0;block<2;block++) {
        append("%s[",block?",":"");
        for(i=block*40;i<call_count && i<(block+1)*40;i++) { CALL *c=&calls[i];
            append("%s[%u,%u,%u,%lld,%lu,%lu]",i>block*40?",":"",c->api,c->detail,c->state,(long long)c->raw,c->error,c->length); }
        append("]");
    }
    append("],\"ipc_write_calls_planned\":2,\"node_bound\":%u,\"frame_budget\":8192}",node_bound());
}
static BOOL room(unsigned bytes,unsigned nodes,unsigned extra_calls) {
    if(problem_code || boundary_failed) return FALSE;
    render();
    if(!frame_ok || used+bytes+FINAL_BYTES>FRAME_BUDGET || node_bound()+nodes+FINAL_NODES>NODE_BUDGET
            || call_count+extra_calls+pending_closes()+2>CALL_BUDGET) { problem(P_BUDGET); return FALSE; }
    return TRUE;
}
static BOOL sid_decode(const BYTE *base,size_t bytes,size_t offset,char text[192],
                       ULONGLONG *authority,BYTE *count,DWORD *first,size_t *length) {
    const BYTE *s; unsigned i; int n,p;
    if(offset%4 || offset>bytes || bytes-offset<8) return FALSE;
    s=base+offset; if(s[0]!=1 || s[1]>15) return FALSE;
    *count=s[1]; *length=8+4*(size_t)*count; if(*length>bytes-offset) return FALSE;
    *authority=0; *first=0; for(i=2;i<8;i++) *authority=(*authority<<8)|s[i];
    if(*count) *first=u32(s+8);
    p=snprintf(text,192,"S-1-%llu",(unsigned long long)*authority);
    if(p<0 || p>=192) return FALSE;
    for(i=0;i<*count;i++) { n=snprintf(text+p,192-(size_t)p,"-%lu",(unsigned long)u32(s+8+4*i));
        if(n<0 || n>=192-p) { text[0]=0; return FALSE; } p+=n; }
    return TRUE;
}
static BOOL token_sid(const void *pointer,size_t header,char text[192],ULONGLONG *authority,BYTE *count,DWORD *first) {
    uintptr_t base=(uintptr_t)token_buffer,address=(uintptr_t)pointer; size_t length;
    if(address<base || address-base<header || address-base>received) return FALSE;
    return sid_decode(token_buffer,received,(size_t)(address-base),text,authority,count,first,&length);
}
static BOOL token_query(TOKEN_INFORMATION_CLASS kind,DWORD minimum,DWORD exact) {
    CALL *c=entered(TOKEN_QUERY_CALL,(unsigned)kind); BOOL raw; DWORD error;
    if(!c) return FALSE;
    received=0; SetLastError(0); raw=GetTokenInformation(GetCurrentProcessToken(),kind,token_buffer,sizeof(token_buffer),&received); error=GetLastError();
    returned(c,raw,error,received);
    if(!raw || received<minimum || received>sizeof(token_buffer) || (exact && received!=exact)) { problem(P_TOKEN); return FALSE; }
    return TRUE;
}
static void close_thread_once(void) {
    CALL *c; BOOL raw; DWORD error;
    if(!thread_partial || thread_close_attempted) return;
    thread_close_attempted=TRUE;
    if(!real_handle(thread_partial)) { ++refused_handles; problem(P_HANDLE); return; }
    c=entered(THREAD_CLOSE,0); if(!c) return;
    SetLastError(0); raw=CloseHandle(thread_partial); error=GetLastError(); returned(c,raw,error,0);
    if(raw) thread_partial=NULL; else problem(P_CLOSE);
}
static int capture_token(void) {
    TOKEN_SNAPSHOT s; TOKEN_USER user; TOKEN_MANDATORY_LABEL label; TOKEN_STATISTICS statistics;
    PSID package; CALL *c; BOOL raw; DWORD error,first; ULONGLONG authority; BYTE count; int result=-1; char il_sid[192];
    if(!room(1024+10*112,36+10*7,10)) return -1;
    memset(&s,0,sizeof(s)); thread_partial=NULL; thread_close_attempted=FALSE;
    c=entered(THREAD_OPEN,gate_count+1); if(!c) return -1;
    SetLastError(0); raw=OpenThreadToken(GetCurrentThread(),TOKEN_QUERY,TRUE,&thread_partial); error=GetLastError();
    returned(c,raw,error,thread_partial!=NULL);
    s.thread_absent=!raw && error==ERROR_NO_TOKEN && !thread_partial;
    if(!s.thread_absent) { problem(P_THREAD); close_thread_once(); goto done; }
    if(!token_query(TokenType,4,4)) goto done; s.type=u32(token_buffer);
    if(s.type!=TokenPrimary) { problem(P_TOKEN); goto done; }
    if(!token_query(TokenUser,sizeof(user),0)) goto done; memcpy(&user,token_buffer,sizeof(user)); s.user_attrs=user.User.Attributes;
    if(!token_sid(user.User.Sid,sizeof(user),s.user,&authority,&count,&first)) { problem(P_POINTER); goto done; }
    if(!token_query(TokenAppContainerSid,sizeof(package),0)) goto done; memcpy(&package,token_buffer,sizeof(package));
    if(!token_sid(package,sizeof(package),s.package,&authority,&count,&first) || authority!=15 || count!=8 || first!=2) { problem(P_POINTER); goto done; }
    if(!token_query(TokenStatistics,sizeof(statistics),sizeof(statistics))) goto done;
    memcpy(&statistics,token_buffer,sizeof(statistics)); s.id=statistics.TokenId;
    if(statistics.TokenType!=TokenPrimary) { problem(P_TOKEN); goto done; }
    if(!token_query(TokenIsAppContainer,4,4)) goto done; s.ac=u32(token_buffer);
    if(s.ac!=1) { problem(P_TOKEN); goto done; }
    if(!token_query(TokenCapabilities,8,0)) goto done; s.cap=u32(token_buffer);
    if(s.cap) { problem(P_TOKEN); goto done; }
    if(!token_query(TokenIntegrityLevel,sizeof(label),0)) goto done; memcpy(&label,token_buffer,sizeof(label));
    if(!token_sid(label.Label.Sid,sizeof(label),il_sid,&authority,&count,&first) || authority!=16 || count!=1
            || !(label.Label.Attributes&SE_GROUP_INTEGRITY)) { problem(P_POINTER); goto done; }
    s.il_known=TRUE; s.il=first; s.il_attrs=label.Label.Attributes;
    if(s.il!=(mode==1?4096u:0u)) { problem(P_TOKEN); goto done; }
    if(!token_query(TokenMandatoryPolicy,4,4)) goto done; s.policy=u32(token_buffer);
    if(s.policy&~3u) { problem(P_TOKEN); goto done; } s.policy_known=TRUE;
    s.valid=s.type==TokenPrimary && s.ac==1 && !s.cap && s.il==(mode==1?4096u:0u) && (s.policy&1);
    if(!s.valid) problem(P_TOKEN);
done:
    if(token_count && !memcmp(&s,&tokens[0],sizeof(s))) result=0;
    else if(token_count<2) { result=(int)token_count; tokens[token_count++]=s; }
    else problem(P_BUDGET);
    if(result>0) problem(P_IDENTITY);
    return result; /* Failed snapshots keep a gate-visible evidence reference. */
}
static BOOL parse_sd(SD_SNAPSHOT *s,BOOL root) {
    DWORD owner,dacl,sacl,first,mask; size_t owner_length,length,offset,ace_length,end; unsigned i;
    ULONGLONG authority; BYTE count; char sid[192]; BOOL user_allow=FALSE,system_allow=FALSE;
    if(s->bytes<20 || sd_buffer[0]!=1) return FALSE;
    s->control=u16(sd_buffer+2); owner=u32(sd_buffer+4); sacl=u32(sd_buffer+12); dacl=u32(sd_buffer+16);
    if(!(s->control&0x8000) || !(s->control&4) || owner<20 || dacl<20
            || dacl%4 || dacl>s->bytes-8
            || !sid_decode(sd_buffer,s->bytes,owner,s->owner,&authority,&count,&first,&owner_length)) return FALSE;
    s->dacl_bytes=u16(sd_buffer+dacl+2); s->dacl_count=u16(sd_buffer+dacl+4); s->dacl_revision=sd_buffer[dacl];
    if(s->dacl_bytes<8 || s->dacl_bytes>s->bytes-dacl) return FALSE;
    if(s->dacl_bytes<=ACL_BYTES) { memcpy(s->dacl,sd_buffer+dacl,s->dacl_bytes); s->dacl_complete=TRUE; }
    if(!(s->control&0x10) || sacl<20 || sacl%4 || sacl>s->bytes-8) return FALSE;
    s->label_bytes=u16(sd_buffer+sacl+2); s->label_count=u16(sd_buffer+sacl+4);
    if(s->label_bytes<8 || s->label_bytes>s->bytes-sacl) return FALSE;
    if(s->label_bytes<=LABEL_BYTES) { memcpy(s->label,sd_buffer+sacl,s->label_bytes); s->label_complete=TRUE; }
    if(!s->dacl_complete || !s->label_complete || s->dacl_count>ACE_LIMIT || s->label_count!=1
            || (s->dacl_revision!=2 && s->dacl_revision!=4) || (sd_buffer[sacl]!=2 && sd_buffer[sacl]!=4)
            || strcmp(s->owner,tokens[0].user)) return FALSE;
    if(!(owner+owner_length<=dacl || owner>=dacl+s->dacl_bytes)
            || !(owner+owner_length<=sacl || owner>=sacl+s->label_bytes)
            || !(dacl+s->dacl_bytes<=sacl || sacl+s->label_bytes<=dacl)) return FALSE;
    offset=dacl+8; end=dacl+s->dacl_bytes;
    for(i=0;i<s->dacl_count;i++) {
        BYTE type,flags;
        if(offset>end || end-offset<16) return FALSE;
        type=sd_buffer[offset]; flags=sd_buffer[offset+1]; ace_length=u16(sd_buffer+offset+2); mask=u32(sd_buffer+offset+4);
        if(type>1 || flags&~0x3f || ace_length<16 || ace_length>end-offset
                || !sid_decode(sd_buffer,offset+ace_length,offset+8,sid,&authority,&count,&first,&length)
                || 8+length!=ace_length || !strcmp(sid,"S-1-3-4")) return FALSE;
        if(strcmp(sid,tokens[0].package) && strcmp(sid,tokens[0].user) && strcmp(sid,"S-1-5-18")
                && strcmp(sid,"S-1-5-32-544")) return FALSE;
        if(!strcmp(sid,tokens[0].package)) {
            ++s->package_count; s->package_flags=flags; s->package_mask=mask;
            if(type || mask!=0xF003F || (root ? flags!=0x23 : !(flags&0x10) || (flags&8))) return FALSE;
        }
        if(!type && mask==0xF003F && !(flags&8)) {
            if(!strcmp(sid,tokens[0].user)) user_allow=TRUE;
            if(!strcmp(sid,"S-1-5-18")) system_allow=TRUE;
        }
        offset+=ace_length;
    }
    if(offset!=end || s->package_count!=1 || !user_allow || !system_allow) return FALSE;
    offset=sacl+8; end=sacl+s->label_bytes;
    if(end-offset<20 || sd_buffer[offset]!=SYSTEM_MANDATORY_LABEL_ACE_TYPE
            || u16(sd_buffer+offset+2)!=end-offset || (sd_buffer[offset+1]&INHERIT_ONLY_ACE)
            || (sd_buffer[offset+1]&~0x17)
            || !sid_decode(sd_buffer,end,offset+8,sid,&authority,&count,&first,&length)
            || offset+8+length!=end || authority!=16 || count!=1) return FALSE;
    s->il=first; s->label_mask=u32(sd_buffer+offset+4); s->label_flags=sd_buffer[offset+1];
    return s->il==4096 && s->label_mask==SYSTEM_MANDATORY_LABEL_NO_WRITE_UP;
}
static int capture_sd(HKEY key,BOOL root) {
    SD_SNAPSHOT s; PSECURITY_DESCRIPTOR descriptor=NULL; HLOCAL freed; CALL *c; DWORD status,error,length=0; unsigned i; int result=-1;
    if(security_count>=4 || !room(2048+3*112,46+3*7,3)) { problem(P_BUDGET); return -1; }
    memset(&s,0,sizeof(s)); c=entered(SECURITY_QUERY,root?1:2); if(!c) return -1;
    SetLastError(0); status=GetSecurityInfo(key,SE_REGISTRY_KEY,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION|LABEL_SECURITY_INFORMATION,
        NULL,NULL,NULL,NULL,&descriptor); error=GetLastError(); returned(c,status,error,descriptor!=NULL);
    if(!status && descriptor) {
        c=entered(SD_LENGTH,0);
        if(c) { SetLastError(0); length=GetSecurityDescriptorLength(descriptor); error=GetLastError(); returned(c,length,error,length); }
        s.bytes=length;
        if(length>=20 && length<=SD_BUFFER) { memcpy(sd_buffer,descriptor,length); s.valid=parse_sd(&s,root); }
    }
    if(!s.valid) { s.reason=P_SD; problem(P_SD); }
    if(descriptor) {
        c=entered(SD_FREE,0);
        if(c) { SetLastError(0); freed=LocalFree(descriptor); error=GetLastError(); returned(c,(int64_t)(intptr_t)freed,error,0);
            if(freed) { unreleased_descriptor=descriptor; release_failed=TRUE; problem(P_CLOSE); } }
        else { unreleased_descriptor=descriptor; release_failed=TRUE; }
        /* One free attempt only; failed ownership is retained until exit. */
    }
    for(i=0;i<security_count;i++) if(!memcmp(&s,&security[i],sizeof(s))) { result=(int)i; break; }
    if(result<0) { result=(int)security_count; security[security_count++]=s; }
    return result;
}
static BOOL gate(unsigned step,KEY_OWNER *child,int expected_child) {
    GATE *g; int t,r,s=-1; unsigned first=call_count;
    if(gate_count>=4) { problem(P_BUDGET); return FALSE; }
    t=capture_token();
    g=&gates[gate_count++]; g->step=step; g->first_call=first; g->token=t; g->root=-1; g->child=-1;
    if(t<0 || problem_code) goto failed;
    if(!root_key.value) {
        CALL *c; HRESULT hr; DWORD error;
        if(!room(112,7,2)) goto failed;
        c=entered(ROOT_OPEN,READ_CONTROL); if(!c) goto failed; ++root_key.generation;
        SetLastError(0); hr=GetAppContainerRegistryLocation(READ_CONTROL,&root_key.value); error=GetLastError(); returned(c,(LONG)hr,error,root_key.value!=NULL);
        if(hr || !real_handle(root_key.value)) { problem(P_ROOT); goto failed; }
    }
    r=capture_sd(root_key.value,TRUE); g->root=r;
    if(r<0 || problem_code) goto failed;
    if(root_reference<0) root_reference=r;
    if(r!=root_reference) { problem(P_SD_CHANGED); goto failed; }
    if(child) {
        s=capture_sd(child->value,FALSE); g->child=s;
        if(s<0 || problem_code) goto failed;
        if(s!=expected_child) { problem(P_SD_CHANGED); goto failed; }
    }
    g->last_call=call_count; return TRUE;
failed:
    g->last_call=call_count; return FALSE;
}
static BOOL close_key_once(KEY_OWNER *owner) {
    CALL *c; LSTATUS status; DWORD error;
    if(!owner->value) return TRUE;
    if(owner->attempted) return FALSE;
    owner->attempted=TRUE;
    if(!real_handle(owner->value)) { ++refused_handles; problem(P_HANDLE); return FALSE; }
    c=entered(KEY_CLOSE,owner->role); if(!c) return FALSE;
    SetLastError(0); status=RegCloseKey(owner->value); error=GetLastError(); returned(c,status,error,owner->generation);
    if(status) { problem(P_CLOSE); return FALSE; } owner->value=NULL; return TRUE;
}
static BOOL prepare_owner(KEY_OWNER *owner) {
    if(owner->value) { problem(P_HANDLE); return FALSE; }
    owner->attempted=FALSE; ++owner->generation; return TRUE;
}
static BOOL create_key(KEY_OWNER *owner,const wchar_t *name,BOOL negative) {
    CALL *c; DWORD disposition=0,error; LSTATUS status;
    if(!room(2*112,14,2) || !prepare_owner(owner)) return FALSE;
    c=entered(KEY_CREATE,owner->role); if(!c) return FALSE;
    SetLastError(0); status=RegCreateKeyExW(root_key.value,name,0,NULL,REG_OPTION_NON_VOLATILE,
        READ_CONTROL|KEY_QUERY_VALUE|KEY_SET_VALUE,NULL,&owner->value,&disposition); error=GetLastError(); returned(c,status,error,disposition);
    if(negative) {
        if(!status) {
            boundary_failed=TRUE; /* Successful KEY_SET_VALUE grant is sticky even on collision. */
            if(disposition!=REG_CREATED_NEW_KEY) problem(P_COLLISION);
            if(!real_handle(owner->value)) problem(P_HANDLE);
        }
        else if(status!=ERROR_ACCESS_DENIED || owner->value || disposition) problem(P_REGISTRY);
        return status==ERROR_ACCESS_DENIED && !owner->value && !problem_code;
    }
    if(status || !real_handle(owner->value)) { problem(P_REGISTRY); return FALSE; }
    if(disposition!=REG_CREATED_NEW_KEY) { problem(P_COLLISION); return FALSE; }
    return TRUE;
}
static BOOL open_seed(BOOL write) {
    KEY_OWNER *owner=write?&write_key:&seed_key; DWORD access=write?KEY_SET_VALUE:(READ_CONTROL|KEY_QUERY_VALUE),error; LSTATUS status; CALL *c;
    if(!room(2*112,14,2) || !prepare_owner(owner)) return FALSE;
    c=entered(KEY_OPEN,write?4:2); if(!c) return FALSE;
    SetLastError(0); status=RegOpenKeyExW(root_key.value,seed_name,0,access,&owner->value); error=GetLastError(); returned(c,status,error,owner->value!=NULL);
    if(write) {
        if(!status) boundary_failed=TRUE; /* Before any query/close; never set. */
        else if(status!=ERROR_ACCESS_DENIED || owner->value) problem(P_REGISTRY);
        return status==ERROR_ACCESS_DENIED && !owner->value && !problem_code;
    }
    if(status || !real_handle(owner->value)) { problem(P_REGISTRY); return FALSE; } return TRUE;
}
static BOOL read_canary(void) {
    BYTE bytes[8]={0}; DWORD size=8,type=0,error; LSTATUS status; CALL *c; CANARY_READ *receipt;
    if(canary_read_count>=3) { problem(P_BUDGET); return FALSE; }
    if(!room(112+128,7+6,1)) return FALSE;
    receipt=&canary_reads[canary_read_count++]; receipt->call_index=call_count;
    c=entered(VALUE_QUERY,REG_BINARY); if(!c) return FALSE;
    SetLastError(0); status=RegQueryValueExW(seed_key.value,L"synthetic",NULL,&type,bytes,&size); error=GetLastError(); returned(c,status,error,size);
    c->detail=type; receipt->type=type; receipt->bytes=size; memcpy(receipt->buffer,bytes,8);
    if(status || type!=REG_BINARY || size!=8 || memcmp(bytes,canary,8)) { problem(P_CANARY); return FALSE; } return TRUE;
}
static BOOL delete_key(const wchar_t *name,BOOL negative) {
    CALL *c; DWORD error; LSTATUS status;
    if(!room(112,7,1)) return FALSE;
    c=entered(KEY_DELETE,negative?2:3); if(!c) return FALSE;
    SetLastError(0); status=RegDeleteKeyW(root_key.value,name); error=GetLastError(); returned(c,status,error,0);
    if(negative && !status) boundary_failed=TRUE;
    if(status!=(negative?ERROR_ACCESS_DENIED:ERROR_SUCCESS)) { if(!boundary_failed) problem(P_REGISTRY); return FALSE; } return TRUE;
}
static BOOL seed_check(GATE *g) {
    int sd; BOOL ok;
    if(!open_seed(FALSE)) return FALSE;
    sd=capture_sd(seed_key.value,FALSE); g->child=sd; g->last_call=call_count;
    if(sd<0 || problem_code) return FALSE;
    if(seed_reference<0) seed_reference=sd;
    if(sd!=seed_reference) { problem(P_SD_CHANGED); return FALSE; }
    ok=read_canary() && close_key_once(&seed_key); g->last_call=call_count; return ok;
}
static void positive(void) {
    CALL *c; DWORD error; LSTATUS status; int sd;
    if(!gate(1,NULL,-1) || !create_key(&seed_key,seed_name,FALSE)) return;
    seed_reference=capture_sd(seed_key.value,FALSE); if(seed_reference<0 || problem_code) return;
    if(!gate(2,&seed_key,seed_reference) || !room(112,7,1)) return;
    c=entered(VALUE_SET,REG_BINARY); if(!c) return;
    SetLastError(0); status=RegSetValueExW(seed_key.value,L"synthetic",0,REG_BINARY,canary,8); error=GetLastError(); returned(c,status,error,8);
    if(status) { problem(P_REGISTRY); return; }
    if(!read_canary() || !close_key_once(&seed_key) || !gate(3,NULL,-1) || !create_key(&disposable_key,disposable_name,FALSE)) return;
    disposable_reference=capture_sd(disposable_key.value,FALSE); if(disposable_reference<0 || problem_code) return;
    if(!gate(4,&disposable_key,disposable_reference) || !close_key_once(&disposable_key) || !delete_key(disposable_name,FALSE)) return;
    sd=capture_sd(root_key.value,TRUE); if(sd<0 || problem_code) return;
    if(sd!=root_reference) { problem(P_SD_CHANGED); return; } complete=TRUE;
}
static void negative(void) {
    unsigned step; int sd;
    for(step=1;step<=3;step++) {
        if(!gate(step,NULL,-1) || !seed_check(&gates[gate_count-1])) return;
        if(step==1 && !create_key(&write_key,attempt_name,TRUE)) return;
        if(step==2 && !open_seed(TRUE)) return;
        if(step==3 && !delete_key(seed_name,TRUE)) return;
    }
    sd=capture_sd(root_key.value,TRUE); if(sd<0 || problem_code) return;
    if(sd!=root_reference) { problem(P_SD_CHANGED); return; } complete=TRUE;
}
static int lower_hex(wchar_t c) { return c>=L'0' && c<=L'9' ? c-L'0' : c>=L'a' && c<=L'f' ? c-L'a'+10 : -1; }
static BOOL name_ok(const wchar_t *name,const wchar_t *prefix) {
    size_t i,n=wcslen(prefix);
    if(wcslen(name)!=n+32 || wcsncmp(name,prefix,n)) return FALSE;
    for(i=n;i<n+32;i++) if(lower_hex(name[i])<0) return FALSE;
    return TRUE;
}
int wmain(int argc,wchar_t **argv) {
    HANDLE ipc; wchar_t *end; unsigned long long number; unsigned i; DWORD written; BYTE header[4]; CALL *c;
    if(argc!=7 || argv[1][0]<L'1' || argv[1][0]>L'9' || wcslen(argv[1])>20) return 90;
    errno=0; number=_wcstoui64(argv[1],&end,10); if(errno || *end || !number || number>UINTPTR_MAX) return 90;
    ipc=(HANDLE)(uintptr_t)number; if(!real_handle(ipc)) return 90;
    mode=!wcscmp(argv[2],L"seed-low")?1:!wcscmp(argv[2],L"deny-untrusted")?2:0;
    if(!mode || !name_ok(argv[3],L"seed_") || !name_ok(argv[4],L"disposable_") || !name_ok(argv[5],L"attempt_")
            || wmemcmp(argv[3]+5,argv[4]+11,32) || wmemcmp(argv[3]+5,argv[5]+8,32) || wcslen(argv[6])!=16) return 90;
    for(i=0;i<16;i++) { int n=lower_hex(argv[6][i]); if(n<0) return 90; canary_hex[i]=(char)argv[6][i];
        if(!(i&1)) canary[i/2]=(BYTE)(n<<4); else canary[i/2]|=(BYTE)n; }
    for(i=0;i<32;i++) nonce_hex[i]=(char)argv[3][5+i];
    seed_name=argv[3]; disposable_name=argv[4]; attempt_name=argv[5];
    if(mode==1) positive(); else negative();
    /* Necessary cleanup only, each owner once; unknown never starts new probes. */
    close_key_once(&write_key); close_key_once(&disposable_key); close_key_once(&seed_key); close_key_once(&root_key); close_thread_once();
    render();
    if(!frame_ok || used>FRAME_BUDGET || node_bound()>NODE_BUDGET || call_count+2>CALL_BUDGET) return 91;
    header[0]=(BYTE)(used>>24); header[1]=(BYTE)(used>>16); header[2]=(BYTE)(used>>8); header[3]=(BYTE)used;
    c=entered(IPC_WRITE,1); if(!c) return 91;
    if(!WriteFile(ipc,header,4,&written,NULL) || written!=4) return 92;
    c=entered(IPC_WRITE,2); if(!c) return 91;
    if(!WriteFile(ipc,frame,(DWORD)used,&written,NULL) || written!=used) return 93;
    return boundary_failed?10:!complete || problem_code || root_key.value || seed_key.value || disposable_key.value || write_key.value
        || thread_partial || unreleased_descriptor || release_failed || refused_handles ? 2:0;
}
