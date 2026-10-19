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
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <wchar.h>
#include <errno.h>

/* Compile-time include harness only. Every fixture Win32 API is replaced;
 * no real registry/token/security/IPC object is opened or modified. The
 * harness emits CRT stdout receipts containing the exact fake IPC bytes.
 * This source is prepared only; compilation/execution needs separate approval.
 * Included fixture SHA256:
 * 3f39f093e791e6fbfd3dec45277b3c8fb93585e3abf4a224373be148d89dd6cc.
 */
#define CHECK(c) do { if(!(c)) { fprintf(stderr,"mock assertion line %u\n",(unsigned)__LINE__); exit(97); } } while(0)
static HANDLE WINAPI h_token(void),h_thread(void);
static VOID WINAPI h_set_error(DWORD);
static DWORD WINAPI h_get_error(void);
static BOOL WINAPI h_query(HANDLE,TOKEN_INFORMATION_CLASS,LPVOID,DWORD,PDWORD);
static BOOL WINAPI h_thread_open(HANDLE,DWORD,BOOL,PHANDLE);
static BOOL WINAPI h_thread_close(HANDLE);
static HRESULT WINAPI h_root(REGSAM,PHKEY);
static DWORD WINAPI h_security(HANDLE,SE_OBJECT_TYPE,SECURITY_INFORMATION,PSID*,PSID*,PACL*,PACL*,PSECURITY_DESCRIPTOR*);
static DWORD WINAPI h_length(PSECURITY_DESCRIPTOR);
static HLOCAL WINAPI h_free(HLOCAL);
static LSTATUS WINAPI h_create(HKEY,LPCWSTR,DWORD,LPWSTR,DWORD,REGSAM,const SECURITY_ATTRIBUTES*,PHKEY,PDWORD);
static LSTATUS WINAPI h_open(HKEY,LPCWSTR,DWORD,REGSAM,PHKEY);
static LSTATUS WINAPI h_set(HKEY,LPCWSTR,DWORD,DWORD,const BYTE*,DWORD);
static LSTATUS WINAPI h_read(HKEY,LPCWSTR,LPDWORD,LPDWORD,LPBYTE,LPDWORD);
static LSTATUS WINAPI h_delete(HKEY,LPCWSTR);
static LSTATUS WINAPI h_close(HKEY);
static BOOL WINAPI h_write(HANDLE,LPCVOID,DWORD,LPDWORD,LPOVERLAPPED);
#undef GetCurrentProcessToken
#undef GetCurrentThread
#define GetCurrentProcessToken h_token
#define GetCurrentThread h_thread
#define SetLastError h_set_error
#define GetLastError h_get_error
#define GetTokenInformation h_query
#define OpenThreadToken h_thread_open
#define CloseHandle h_thread_close
#define GetAppContainerRegistryLocation h_root
#define GetSecurityInfo h_security
#define GetSecurityDescriptorLength h_length
#define LocalFree h_free
#define RegCreateKeyExW h_create
#define RegOpenKeyExW h_open
#define RegSetValueExW h_set
#define RegQueryValueExW h_read
#define RegDeleteKeyW h_delete
#define RegCloseKey h_close
#define WriteFile h_write
#define H_FORBIDDEN() (fprintf(stderr,"forbidden fixture API\n"),exit(98),0)
#define OpenProcessToken(...) H_FORBIDDEN()
#define SetTokenInformation(...) H_FORBIDDEN()
#define SetSecurityInfo(...) H_FORBIDDEN()
#define RegSetKeySecurity(...) H_FORBIDDEN()
#define AdjustTokenPrivileges(...) H_FORBIDDEN()
#define ImpersonateLoggedOnUser(...) H_FORBIDDEN()
#define DuplicateTokenEx(...) H_FORBIDDEN()
#define CreateProcessW(...) H_FORBIDDEN()
#define CreateFileW(...) ((HANDLE)(uintptr_t)H_FORBIDDEN())
#define RegEnumKeyExW(...) H_FORBIDDEN()
#define RegEnumValueW(...) H_FORBIDDEN()
#define wmain included_fixture_wmain
#include "native_registry_integrity_probe_v5.c"
#undef wmain

enum { H_GOOD,H_NO_LABEL,H_LOW_LABEL_BAD,H_ACE_UNKNOWN,H_CHILD_BAD,H_COLLISION,H_PARTIAL_CREATE,
    H_ROOT_PARTIAL,H_THREAD,H_THREAD_PSEUDO,H_TOKEN_CHANGE,H_TOKEN_POINTER,H_TOKEN_LENGTH,H_CAP,
    H_FREE_FAIL,H_CREATE_SUCCESS,H_CREATE_COLLISION,H_OPEN_SUCCESS,H_DELETE_SUCCESS,H_QUERY_DATA,
    H_QUERY_TYPE,H_QUERY_MORE,H_CLOSE_FAIL,H_BOUNDARY_CLOSE_FAIL,H_SD_CHANGE,H_ACL_OVERSIZE,
    H_BAD_NAME,H_BAD_NONCE,H_BAD_HEX,H_BAD_MODE,H_SECURITY_PARTIAL,H_SID_UNKNOWN,H_LABEL_IO,H_LABEL_UNKNOWN,H_CAP_SHORT,H_ADMIN_ALLOW };
typedef struct { const char *name; unsigned mode,kind; int expected_exit; BOOL boundary,unknown; } CASE;
static const CASE cases[]={
    {"low-positive",1,H_GOOD,0,0,0},{"untrusted-denials",2,H_GOOD,0,0,0},
    {"missing-label",1,H_NO_LABEL,2,0,1},{"untrusted-label-object",1,H_LOW_LABEL_BAD,2,0,1},
    {"unknown-ace",1,H_ACE_UNKNOWN,2,0,1},{"created-child-bad-label",1,H_CHILD_BAD,2,0,1},
    {"existing-seed-refused",1,H_COLLISION,2,0,1},{"failed-create-partial-hkey",1,H_PARTIAL_CREATE,2,0,1},
    {"failed-root-partial-hkey",1,H_ROOT_PARTIAL,2,0,1},{"unexpected-thread-token",1,H_THREAD,2,0,1},
    {"thread-pseudo-refused",1,H_THREAD_PSEUDO,2,0,1},{"next-gate-token-changed",1,H_TOKEN_CHANGE,2,0,1},
    {"token-sid-pointer",1,H_TOKEN_POINTER,2,0,1},{"token-result-length",1,H_TOKEN_LENGTH,2,0,1},
    {"capability-count",1,H_CAP,2,0,1},{"descriptor-free-failed",1,H_FREE_FAIL,2,0,1},
    {"untrusted-create-succeeded",2,H_CREATE_SUCCESS,10,1,0},{"untrusted-create-collision",2,H_CREATE_COLLISION,10,1,0},
    {"untrusted-write-open-succeeded",2,H_OPEN_SUCCESS,10,1,0},{"untrusted-delete-succeeded",2,H_DELETE_SUCCESS,10,1,0},
    {"seed-value-mismatch",2,H_QUERY_DATA,2,0,1},{"seed-type-mismatch",2,H_QUERY_TYPE,2,0,1},
    {"seed-more-data-no-retry",2,H_QUERY_MORE,2,0,1},{"root-close-failed-no-retry",2,H_CLOSE_FAIL,2,0,1},
    {"boundary-then-close-failed",2,H_BOUNDARY_CLOSE_FAIL,10,1,0},{"root-sd-changed",1,H_SD_CHANGE,2,0,1},
    {"oversize-dacl",1,H_ACL_OVERSIZE,2,0,1},{"argument-path-refused",1,H_BAD_NAME,90,0,1},
    {"argument-nonce-refused",1,H_BAD_NONCE,90,0,1},{"argument-hex-refused",1,H_BAD_HEX,90,0,1},
    {"argument-mode-refused",1,H_BAD_MODE,90,0,1},{"failed-security-partial-descriptor",1,H_SECURITY_PARTIAL,2,0,1},
    {"unknown-trustee",1,H_SID_UNKNOWN,2,0,1},{"inherit-only-label",1,H_LABEL_IO,2,0,1},
    {"unknown-label-flag",1,H_LABEL_UNKNOWN,2,0,1},{"short-capabilities-header",1,H_CAP_SHORT,2,0,1},
    {"known-administrators-trustee",1,H_ADMIN_ALLOW,0,0,0}
};
static const CASE *h_case;
static DWORD h_error,h_sd_size;
static unsigned h_native,h_threads,h_creates,h_sets,h_deletes,h_root_sds,h_frees,h_writes,h_bytes;
static unsigned h_key_closes[4];
static BOOL h_seed_exists,h_seed_written,h_disposable_exists,h_attempt_exists,h_sd_outstanding;
static BYTE h_sd[4096],h_ipc[FRAME_BUDGET+4],h_value[8];
static const DWORD h_user_sub[]={21,1,2,3,1001},h_package_sub[]={2,1,2,3,4,5,6,7},h_system_sub[]={18},h_other_sub[]={19},h_admin_sub[]={32,544};
static const wchar_t h_seed[]=L"seed_0123456789abcdef0123456789abcdef";
static const wchar_t h_disposable[]=L"disposable_0123456789abcdef0123456789abcdef";
static const wchar_t h_attempt[]=L"attempt_0123456789abcdef0123456789abcdef";
static void h_enter(void) { ++h_native; CHECK(h_native==call_count && h_native<=CALL_BUDGET); }
static HANDLE WINAPI h_token(void) { return (HANDLE)(intptr_t)-4; }
static HANDLE WINAPI h_thread(void) { return (HANDLE)(intptr_t)-2; }
static VOID WINAPI h_set_error(DWORD e) { h_error=e; }
static DWORD WINAPI h_get_error(void) { return h_error; }
static void h_u16(BYTE *p,WORD v) { memcpy(p,&v,2); }
static void h_u32(BYTE *p,DWORD v) { memcpy(p,&v,4); }
static DWORD h_sid(BYTE *p,ULONGLONG authority,const DWORD *sub,BYTE count) {
    unsigned i; p[0]=1;p[1]=count;
    for(i=0;i<6;i++) p[2+i]=(BYTE)(authority>>(8*(5-i)));
    memcpy(p+8,sub,4*(size_t)count); return 8+4*(DWORD)count;
}
static BOOL WINAPI h_thread_open(HANDLE h,DWORD access,BOOL self,PHANDLE out) {
    h_enter(); ++h_threads; CHECK(h==h_thread() && access==TOKEN_QUERY && self && out && !*out);
    if(h_case->kind==H_THREAD || h_case->kind==H_THREAD_PSEUDO) {
        *out=h_case->kind==H_THREAD?(HANDLE)(uintptr_t)0x9000:(HANDLE)(intptr_t)-4;
        h_error=5; return h_case->kind==H_THREAD?2:0;
    }
    h_error=1008; return 0;
}
static BOOL WINAPI h_thread_close(HANDLE h) { h_enter(); CHECK(h==(HANDLE)(uintptr_t)0x9000); h_error=777; return 2; }
static BOOL WINAPI h_query(HANDLE h,TOKEN_INFORMATION_CLASS kind,LPVOID data,DWORD capacity,PDWORD got) {
    BYTE *p=(BYTE*)data; DWORD n=4,il=h_case->mode==1?4096u:0u; TOKEN_USER user;
    TOKEN_MANDATORY_LABEL label; TOKEN_STATISTICS stats; PSID package;
    h_enter(); CHECK(h==h_token() && data && capacity==SD_BUFFER && got); memset(data,0,capacity);
    switch(kind) {
    case TokenType:h_u32(p,TokenPrimary);break;
    case TokenUser:
        memset(&user,0,sizeof(user)); user.User.Sid=(PSID)(p+sizeof(user));
        if(h_case->kind==H_TOKEN_POINTER) user.User.Sid=(PSID)(p+capacity+4);
        memcpy(p,&user,sizeof(user)); n=(DWORD)sizeof(user)+h_sid(p+sizeof(user),5,h_user_sub,5);break;
    case TokenAppContainerSid:
        package=(PSID)(p+sizeof(package));memcpy(p,&package,sizeof(package));
        n=(DWORD)sizeof(package)+h_sid(p+sizeof(package),15,h_package_sub,8);break;
    case TokenStatistics:
        memset(&stats,0,sizeof(stats));stats.TokenType=TokenPrimary;stats.TokenId.LowPart=101;
        if(h_case->kind==H_TOKEN_CHANGE && h_threads>1) stats.TokenId.LowPart=102;
        memcpy(p,&stats,sizeof(stats));n=(DWORD)sizeof(stats);break;
    case TokenIsAppContainer:h_u32(p,1);break;
    case TokenCapabilities:h_u32(p,h_case->kind==H_CAP?1u:0u);n=h_case->kind==H_CAP_SHORT?4u:8u;break;
    case TokenIntegrityLevel:
        memset(&label,0,sizeof(label));label.Label.Sid=(PSID)(p+sizeof(label));label.Label.Attributes=0x60;
        memcpy(p,&label,sizeof(label));n=(DWORD)sizeof(label)+h_sid(p+sizeof(label),16,&il,1);
        if(h_case->kind==H_TOKEN_LENGTH)n=8;break;
    case TokenMandatoryPolicy:h_u32(p,1);break;
    default:CHECK(0);
    }
    *got=n; h_error=777; return 2;
}
static HRESULT WINAPI h_root(REGSAM access,PHKEY out) {
    h_enter(); CHECK(access==READ_CONTROL && out && !*out); *out=(HKEY)(uintptr_t)0x100;h_error=777;
    return h_case->kind==H_ROOT_PARTIAL?(HRESULT)0x80070005:S_OK;
}
static DWORD h_ace(BYTE *p,BYTE flags,DWORD mask,ULONGLONG authority,const DWORD *sid,BYTE count) {
    DWORD n=8+h_sid(p+8,authority,sid,count);p[0]=0;p[1]=flags;h_u16(p+2,(WORD)n);h_u32(p+4,mask);return n;
}
static void h_make_sd(BOOL root) {
    DWORD dacl=48,sacl,at=dacl+8,il=4096; BYTE flags=root?2:0x12; WORD control=0x8014;
    memset(h_sd,0,sizeof(h_sd));h_sd[0]=1;h_u32(h_sd+4,20);h_sid(h_sd+20,5,h_user_sub,5);
    at+=h_ace(h_sd+at,root?0x23:0x33,0xF003F,15,h_package_sub,8);
    at+=h_ace(h_sd+at,flags,0xF003F,5,h_user_sub,5);
    at+=h_ace(h_sd+at,flags,0xF003F,5,h_case->kind==H_SID_UNKNOWN?h_other_sub:h_system_sub,1);
    if(h_case->kind==H_ADMIN_ALLOW)at+=h_ace(h_sd+at,flags,0xF003F,5,h_admin_sub,2);
    h_sd[dacl]=2;h_u16(h_sd+dacl+2,(WORD)(at-dacl));h_u16(h_sd+dacl+4,h_case->kind==H_ADMIN_ALLOW?4:3);
    if(h_case->kind==H_ACE_UNKNOWN) h_sd[dacl+8]=5;
    if(h_case->kind==H_ACL_OVERSIZE) {at=dacl+600;h_u16(h_sd+dacl+2,600);}
    sacl=at;h_sd[sacl]=2;h_u16(h_sd+sacl+2,28);h_u16(h_sd+sacl+4,1);
    h_sd[sacl+8]=SYSTEM_MANDATORY_LABEL_ACE_TYPE;h_sd[sacl+9]=flags;h_u16(h_sd+sacl+10,20);
    if(h_case->kind==H_LABEL_IO)h_sd[sacl+9]|=INHERIT_ONLY_ACE;
    if(h_case->kind==H_LABEL_UNKNOWN)h_sd[sacl+9]|=0x20;
    h_u32(h_sd+sacl+12,SYSTEM_MANDATORY_LABEL_NO_WRITE_UP);
    if(h_case->kind==H_LOW_LABEL_BAD || (!root && h_case->kind==H_CHILD_BAD)) il=0;
    h_sid(h_sd+sacl+16,16,&il,1);h_u32(h_sd+12,sacl);h_u32(h_sd+16,dacl);h_sd_size=sacl+28;
    if(h_case->kind==H_NO_LABEL) {control=0x8004;h_u32(h_sd+12,0);h_sd_size=sacl;}
    if(root && h_case->kind==H_SD_CHANGE && h_root_sds>1) control|=0x1000;
    h_u16(h_sd+2,control);
}
static DWORD WINAPI h_security(HANDLE key,SE_OBJECT_TYPE object,SECURITY_INFORMATION info,
    PSID *owner,PSID *group,PACL *dacl,PACL *sacl,PSECURITY_DESCRIPTOR *out) {
    BOOL root=key==(HANDLE)(uintptr_t)0x100;
    h_enter();CHECK((root || key==(HANDLE)(uintptr_t)0x200 || key==(HANDLE)(uintptr_t)0x201)
        && object==SE_REGISTRY_KEY && info==0x15 && !owner && !group && !dacl && !sacl && out && !*out && !h_sd_outstanding);
    if(root)++h_root_sds;h_make_sd(root);*out=(PSECURITY_DESCRIPTOR)h_sd;h_sd_outstanding=TRUE;h_error=777;
    return h_case->kind==H_SECURITY_PARTIAL?5u:0u;
}
static DWORD WINAPI h_length(PSECURITY_DESCRIPTOR sd) {h_enter();CHECK(sd==(PSECURITY_DESCRIPTOR)h_sd && h_sd_outstanding);h_error=777;return h_sd_size;}
static HLOCAL WINAPI h_free(HLOCAL sd) {
    h_enter();++h_frees;CHECK(sd==(HLOCAL)h_sd && h_sd_outstanding);h_error=777;
    if(h_case->kind==H_FREE_FAIL)return sd;h_sd_outstanding=FALSE;return NULL;
}
static LSTATUS WINAPI h_create(HKEY root,LPCWSTR name,DWORD reserved,LPWSTR cls,DWORD options,
    REGSAM access,const SECURITY_ATTRIBUTES *sa,PHKEY out,PDWORD disposition) {
    h_enter();++h_creates;CHECK(root==(HKEY)(uintptr_t)0x100 && !reserved && !cls && options==0
        && access==(READ_CONTROL|KEY_QUERY_VALUE|KEY_SET_VALUE) && !sa && out && !*out && disposition && !*disposition);
    h_error=777;
    if(h_case->mode==2) {
        CHECK(!wcscmp(name,h_attempt));
        if(h_case->kind==H_CREATE_SUCCESS || h_case->kind==H_BOUNDARY_CLOSE_FAIL || h_case->kind==H_CREATE_COLLISION) {
            *out=(HKEY)(uintptr_t)0x202;*disposition=h_case->kind==H_CREATE_COLLISION?2:1;h_attempt_exists=TRUE;return 0; }
        return 5;
    }
    if(!wcscmp(name,h_seed)) {
        *out=(HKEY)(uintptr_t)0x200;h_seed_exists=TRUE;
        if(h_case->kind==H_COLLISION) {*disposition=2;return 0;}
        if(h_case->kind==H_PARTIAL_CREATE)return 5;
    } else {CHECK(!wcscmp(name,h_disposable));*out=(HKEY)(uintptr_t)0x201;h_disposable_exists=TRUE;}
    *disposition=1;return 0;
}
static LSTATUS WINAPI h_open(HKEY root,LPCWSTR name,DWORD options,REGSAM access,PHKEY out) {
    h_enter();CHECK(root==(HKEY)(uintptr_t)0x100 && !wcscmp(name,h_seed) && !options && out && !*out && h_seed_exists);h_error=777;
    if(access==KEY_SET_VALUE) {CHECK(h_case->mode==2);if(h_case->kind==H_OPEN_SUCCESS){*out=(HKEY)(uintptr_t)0x202;return 0;}return 5;}
    CHECK(access==(READ_CONTROL|KEY_QUERY_VALUE));*out=(HKEY)(uintptr_t)0x200;return 0;
}
static LSTATUS WINAPI h_set(HKEY key,LPCWSTR name,DWORD reserved,DWORD type,const BYTE *data,DWORD size) {
    h_enter();++h_sets;CHECK(h_case->mode==1 && key==(HKEY)(uintptr_t)0x200 && !wcscmp(name,L"synthetic") && !reserved
        && type==REG_BINARY && size==8 && h_seed_exists && !memcmp(data,canary,8));
    memcpy(h_value,data,8);h_seed_written=TRUE;h_error=777;return 0;
}
static LSTATUS WINAPI h_read(HKEY key,LPCWSTR name,LPDWORD reserved,LPDWORD type,LPBYTE data,LPDWORD size) {
    h_enter();CHECK(key==(HKEY)(uintptr_t)0x200 && !wcscmp(name,L"synthetic") && !reserved && type && data && size && *size==8
        && h_seed_exists && h_seed_written);memcpy(data,h_value,8);*type=REG_BINARY;h_error=777;
    if(h_case->kind==H_QUERY_DATA)data[0]^=1;
    if(h_case->kind==H_QUERY_TYPE)*type=REG_SZ;
    if(h_case->kind==H_QUERY_MORE){*size=9;return ERROR_MORE_DATA;}
    *size=8;return 0;
}
static LSTATUS WINAPI h_delete(HKEY root,LPCWSTR name) {
    h_enter();++h_deletes;CHECK(root==(HKEY)(uintptr_t)0x100);h_error=777;
    if(h_case->mode==1){CHECK(!wcscmp(name,h_disposable) && h_disposable_exists);h_disposable_exists=FALSE;return 0;}
    CHECK(!wcscmp(name,h_seed) && h_seed_exists);if(h_case->kind==H_DELETE_SUCCESS){h_seed_exists=FALSE;return 0;}return 5;
}
static LSTATUS WINAPI h_close(HKEY key) {
    unsigned role;h_enter();h_error=777;
    role=key==(HKEY)(uintptr_t)0x100?0:key==(HKEY)(uintptr_t)0x200?1:key==(HKEY)(uintptr_t)0x201?2:key==(HKEY)(uintptr_t)0x202?3:4;
    CHECK(role<4);++h_key_closes[role];
    if((h_case->kind==H_CLOSE_FAIL && !role) || (h_case->kind==H_BOUNDARY_CLOSE_FAIL && role==3)) {CHECK(h_key_closes[role]==1);return 5;}
    return 0;
}
static BOOL WINAPI h_write(HANDLE ipc,LPCVOID data,DWORD size,LPDWORD written,LPOVERLAPPED overlapped) {
    h_enter();CHECK(ipc==(HANDLE)(uintptr_t)123 && !overlapped && written && h_writes<2 && h_bytes+size<=sizeof(h_ipc));
    CHECK((h_writes==0 && size==4) || (h_writes==1 && size==used));memcpy(h_ipc+h_bytes,data,size);h_bytes+=size;
    ++h_writes;*written=size;h_error=777;return 2;
}
static void h_reset(const CASE *value) {
    unsigned i;h_case=value;h_error=h_sd_size=h_native=h_threads=h_creates=h_sets=h_deletes=h_root_sds=h_frees=h_writes=h_bytes=0;
    memset(h_key_closes,0,sizeof(h_key_closes));memset(h_ipc,0,sizeof(h_ipc));h_seed_exists=h_seed_written=value->mode==2;
    h_disposable_exists=h_attempt_exists=h_sd_outstanding=FALSE;
    for(i=0;i<8;i++)h_value[i]=(BYTE)i;
    memset(calls,0,sizeof(calls));memset(tokens,0,sizeof(tokens));memset(security,0,sizeof(security));memset(gates,0,sizeof(gates));
    memset(canary_reads,0,sizeof(canary_reads));memset(canary,0,sizeof(canary));memset(canary_hex,0,sizeof(canary_hex));memset(nonce_hex,0,sizeof(nonce_hex));
    root_key.value=seed_key.value=disposable_key.value=write_key.value=NULL;
    root_key.generation=seed_key.generation=disposable_key.generation=write_key.generation=0;
    root_key.attempted=seed_key.attempted=disposable_key.attempted=write_key.attempted=FALSE;
    thread_partial=NULL;unreleased_descriptor=NULL;thread_close_attempted=release_failed=boundary_failed=complete=FALSE;
    call_count=token_count=security_count=gate_count=canary_read_count=problem_code=refused_handles=mode=0;
    root_reference=seed_reference=disposable_reference=-1;used=0;frame_ok=TRUE;
}
int main(void) {
    unsigned i,j;int code;wchar_t *args[7];
    for(i=0;i<sizeof(cases)/sizeof(cases[0]);i++) {
        h_reset(&cases[i]);args[0]=L"fixture";args[1]=L"123";args[2]=cases[i].mode==1?L"seed-low":L"deny-untrusted";
        args[3]=(wchar_t*)h_seed;args[4]=(wchar_t*)h_disposable;args[5]=(wchar_t*)h_attempt;args[6]=L"0001020304050607";
        if(cases[i].kind==H_BAD_NAME)args[3]=L"seed_..\\foreign";
        if(cases[i].kind==H_BAD_NONCE)args[5]=L"attempt_1123456789abcdef0123456789abcdef";
        if(cases[i].kind==H_BAD_HEX)args[6]=L"000102030405060A";
        if(cases[i].kind==H_BAD_MODE)args[2]=L"arbitrary";
        code=included_fixture_wmain(7,args);CHECK(code==cases[i].expected_exit && boundary_failed==cases[i].boundary && call_count==h_native && call_count<=80);
        if(code==90)CHECK(!h_native && !h_writes);
        else {
            CHECK(h_writes==2 && h_bytes==used+4 && used<8192 && node_bound()<=1024);
            CHECK((!boundary_failed && (!complete || problem_code || root_key.value || seed_key.value || disposable_key.value || write_key.value
                || thread_partial || unreleased_descriptor || release_failed || refused_handles))==cases[i].unknown);
            CHECK(h_sets<=1 && (cases[i].mode==1 || !h_sets));
            if(cases[i].kind==H_GOOD || cases[i].kind==H_ADMIN_ALLOW) {CHECK(complete && !problem_code);CHECK(h_native==(cases[i].mode==1?74u:64u));}
            if(cases[i].kind==H_CHILD_BAD || cases[i].kind==H_COLLISION || cases[i].kind==H_PARTIAL_CREATE || cases[i].kind==H_TOKEN_CHANGE || cases[i].kind==H_SD_CHANGE)
                CHECK(h_creates==1 && !h_sets && !h_deletes);
            if(cases[i].kind==H_CREATE_SUCCESS || cases[i].kind==H_BOUNDARY_CLOSE_FAIL || cases[i].kind==H_CREATE_COLLISION)
                CHECK(h_creates==1 && !h_sets && !h_deletes && h_attempt_exists);
            if(cases[i].kind==H_QUERY_MORE || cases[i].kind==H_QUERY_DATA || cases[i].kind==H_QUERY_TYPE)CHECK(!h_creates && !h_deletes);
            if(cases[i].kind==H_SID_UNKNOWN || cases[i].kind==H_LABEL_IO || cases[i].kind==H_LABEL_UNKNOWN || cases[i].kind==H_CAP_SHORT)
                CHECK(!h_creates && !h_sets && !h_deletes);
        }
        printf("{\"case\":\"%s\",\"fixture_exit\":%d,\"native_calls\":%u,\"frame_hex\":\"",cases[i].name,code,h_native);
        for(j=0;j<h_bytes;j++)printf("%02x",(unsigned)h_ipc[j]);printf("\"}\n");
    }
    /* Budget refusals are tested without pretending synthetic rows were real
     * native calls. Both checks must stop before any mutation/API invocation. */
    h_reset(&cases[0]);call_count=77;CHECK(!room(0,0,2) && problem_code==P_BUDGET && !h_native);
    h_reset(&cases[0]);CHECK(!room(FRAME_BUDGET,0,0) && problem_code==P_BUDGET && !h_native);
    puts("{\"preflight_checks\":2,\"native_calls\":0}");return 0;
}
