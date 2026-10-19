#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif
#if _WIN32_WINNT < 0x0602
#error GetCurrentProcessToken requires Windows 8 or later
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <errno.h>

/* Own-token v4 only. Native allowlist: GetCurrentProcessToken (query-only
 * pseudo), GetCurrentProcess/GetCurrentThread, GetTokenInformation,
 * OpenThreadToken, OpenProcessToken, SetTokenInformation, CloseHandle,
 * SetLastError/GetLastError, and two WriteFile calls to the fixed inherited IPC.
 * Call budget counts audited security and IPC calls; pseudo accessors and
 * last-error bookkeeping are not counted. No registry, file-open, network,
 * child, impersonation, privilege adjustment, or other-process-open APIs.
 * One final frame: a missing frame is UNKNOWN, never resistance evidence.
 */
#define CALL_BUDGET 48
#define FRAME_BUDGET 4096
enum { Q = 1, THREAD_OPEN, PROCESS_OPEN, SET_TOKEN, CLOSE_TOKEN, IPC_WRITE };
typedef struct { unsigned api, info, state; LONG raw; DWORD error, length; } CALL;
typedef struct {
    BOOL valid, thread_absent, il_known, policy_known;
    DWORD type, ac, caps, il, il_attrs, policy, user_attrs;
    LUID id;
    char user[192], package[192];
} SNAPSHOT;
typedef struct { TOKEN_MANDATORY_LABEL label; BYTE sid[12]; } LOW_LABEL;
typedef char require_x64[(sizeof(void *) == 8 && sizeof(TOKEN_STATISTICS) == 56) ? 1 : -1];
typedef char require_label_layout[(sizeof(TOKEN_MANDATORY_LABEL) == 16 && offsetof(LOW_LABEL, sid) == 16) ? 1 : -1];
__declspec(align(8)) static BYTE scratch[65536];
static CALL calls[CALL_BUDGET];
static unsigned call_count, problems, refused_handles;
static DWORD received;
static BOOL attack_succeeded, cleanup_ok = TRUE;
static char frame[FRAME_BUDGET];
static size_t used;
static BOOL frame_ok = TRUE;

static CALL *entered(unsigned api, unsigned info) {
    CALL *call;
    if (call_count >= CALL_BUDGET) { problems |= 1; return NULL; }
    call = &calls[call_count++];
    call->api = api; call->info = info; call->state = 1;
    return call;
}

static DWORD u32(const BYTE *address) { DWORD value; memcpy(&value, address, 4); return value; }

static BOOL query(HANDLE token, TOKEN_INFORMATION_CLASS info, DWORD minimum, DWORD exact) {
    CALL *call = entered(Q, (unsigned)info);
    BOOL raw; DWORD error;
    if (!call) return FALSE;
    received = 0;
    SetLastError(0);
    raw = GetTokenInformation(token, info, scratch, sizeof(scratch), &received);
    error = GetLastError();
    call->raw = raw; call->error = error; call->length = received; call->state = 2;
    if (!raw || received < minimum || received > sizeof(scratch) || (exact && received != exact)) {
        problems |= 2; return FALSE;
    }
    return TRUE;
}

static BOOL sid_text(const void *pointer, size_t header, char *text,
                     ULONGLONG *authority, BYTE *count, DWORD *first) {
    uintptr_t base = (uintptr_t)scratch, address = (uintptr_t)pointer;
    size_t offset, length; const BYTE *sid; unsigned index; int n, position;
    if (address < base || address % 4 || address - base < header || address - base > received) goto bad;
    offset = (size_t)(address - base);
    if (received - offset < 8) goto bad;
    sid = scratch + offset;
    if (sid[0] != 1 || sid[1] > 15) goto bad;
    length = 8 + 4 * (size_t)sid[1];
    if (length > received - offset) goto bad;
    *authority = 0; *count = sid[1]; *first = 0;
    for (index = 2; index < 8; ++index) *authority = (*authority << 8) | sid[index];
    if (*count) *first = u32(sid + 8);
    position = snprintf(text, 192, "S-1-%llu", (unsigned long long)*authority);
    if (position < 0 || position >= 192) goto bad;
    for (index = 0; index < *count; ++index) {
        n = snprintf(text + position, 192 - (size_t)position, "-%lu", (unsigned long)u32(sid + 8 + 4 * index));
        if (n < 0 || n >= 192 - position) goto bad;
        position += n;
    }
    return TRUE;
bad:
    text[0] = 0; problems |= 4; return FALSE;
}

static BOOL real_handle(HANDLE handle) {
    uintptr_t value = (uintptr_t)handle;
    return value && !(value >= (uintptr_t)-6);
}

static void close_once(HANDLE *owned, unsigned role) {
    CALL *call; BOOL raw; DWORD error;
    if (!*owned) return;
    if (!real_handle(*owned)) { problems |= 16; ++refused_handles; cleanup_ok = FALSE; return; }
    call = entered(CLOSE_TOKEN, role);
    if (!call) { cleanup_ok = FALSE; return; }
    SetLastError(0);
    raw = CloseHandle(*owned);
    error = GetLastError();
    call->raw = raw; call->error = error; call->state = 2;
    if (!raw) { problems |= 32; cleanup_ok = FALSE; }
    else *owned = NULL;
    /* No retry, including a failed close. The process exits after reporting. */
}

static BOOL absent_thread(unsigned phase) {
    HANDLE partial = NULL; /* Output is owned before the native call. */
    CALL *call = entered(THREAD_OPEN, phase);
    BOOL raw, absent; DWORD error;
    if (!call) return FALSE;
    SetLastError(0);
    raw = OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, TRUE, &partial);
    error = GetLastError();
    call->raw = raw; call->error = error; call->length = partial != NULL; call->state = 2;
    absent = !raw && error == ERROR_NO_TOKEN && !partial;
    if (!absent) problems |= 8;
    close_once(&partial, phase);
    return absent;
}

static BOOL capture(HANDLE token, SNAPSHOT *snapshot, unsigned phase) {
    TOKEN_USER user; TOKEN_MANDATORY_LABEL label; TOKEN_STATISTICS statistics;
    PSID package; ULONGLONG authority; BYTE count; DWORD first; BOOL ok = TRUE;
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->thread_absent = absent_thread(phase);
    if (query(token, TokenType, 4, 4)) snapshot->type = u32(scratch); else ok = FALSE;
    if (query(token, TokenUser, sizeof(user), 0)) {
        memcpy(&user, scratch, sizeof(user)); snapshot->user_attrs = user.User.Attributes;
        if (!sid_text(user.User.Sid, sizeof(user), snapshot->user, &authority, &count, &first)) ok = FALSE;
    } else ok = FALSE;
    if (query(token, TokenAppContainerSid, sizeof(package), 0)) {
        memcpy(&package, scratch, sizeof(package));
        if (!sid_text(package, sizeof(package), snapshot->package, &authority, &count, &first)
                || authority != 15 || count != 8 || first != 2) { problems |= 4; ok = FALSE; }
    } else ok = FALSE;
    if (query(token, TokenStatistics, sizeof(statistics), sizeof(statistics))) {
        memcpy(&statistics, scratch, sizeof(statistics)); snapshot->id = statistics.TokenId;
        if (statistics.TokenType != TokenPrimary) ok = FALSE;
    } else ok = FALSE;
    if (query(token, TokenIsAppContainer, 4, 4)) snapshot->ac = u32(scratch); else ok = FALSE;
    if (query(token, TokenCapabilities, 4, 0)) {
        snapshot->caps = u32(scratch);
        /* Nonzero count is rejected without dereferencing any array entries. */
        if (snapshot->caps != 0) ok = FALSE;
    } else ok = FALSE;
    if (query(token, TokenIntegrityLevel, sizeof(label), 0)) {
        char sid[192]; memcpy(&label, scratch, sizeof(label));
        if (sid_text(label.Label.Sid, sizeof(label), sid, &authority, &count, &first)
                && authority == 16 && count == 1 && (label.Label.Attributes & SE_GROUP_INTEGRITY)) {
            snapshot->il = first; snapshot->il_attrs = label.Label.Attributes; snapshot->il_known = TRUE;
        } else { problems |= 4; ok = FALSE; }
    } else ok = FALSE;
    if (query(token, TokenMandatoryPolicy, 4, 4)) {
        snapshot->policy = u32(scratch);
        if (snapshot->policy & ~TOKEN_MANDATORY_POLICY_VALID_MASK) ok = FALSE;
        else snapshot->policy_known = TRUE;
    } else ok = FALSE;
    snapshot->valid = ok && snapshot->thread_absent && snapshot->type == TokenPrimary
        && snapshot->ac == 1 && snapshot->caps == 0;
    return snapshot->valid;
}

static BOOL same_identity(const SNAPSHOT *before, const SNAPSHOT *after) {
    return before->valid && after->valid && before->type == after->type
        && before->ac == after->ac && before->caps == after->caps
        && before->user_attrs == after->user_attrs && before->il_attrs == after->il_attrs
        && before->id.LowPart == after->id.LowPart && before->id.HighPart == after->id.HighPart
        && !strcmp(before->user, after->user) && !strcmp(before->package, after->package);
}

static void append(const char *format, ...) {
    va_list arguments; int n;
    if (!frame_ok) return;
    va_start(arguments, format); n = vsnprintf(frame + used, sizeof(frame) - used, format, arguments); va_end(arguments);
    if (n < 0 || (size_t)n >= sizeof(frame) - used) { frame_ok = FALSE; return; }
    used += (size_t)n;
}

static void snapshot_json(const SNAPSHOT *s) {
    append("{\"valid\":%d,\"thread_absent\":%d,\"type\":%lu,\"user\":\"%s\",\"package\":\"%s\","
        "\"id\":[%lu,%ld],\"ac\":%lu,\"cap\":%lu,\"il_known\":%d,\"il\":%lu,\"il_attrs\":%lu,"
        "\"policy_known\":%d,\"policy\":%lu,\"user_attrs\":%lu}", s->valid, s->thread_absent,
        s->type, s->user, s->package, s->id.LowPart, s->id.HighPart, s->ac, s->caps,
        s->il_known, s->il, s->il_attrs, s->policy_known, s->policy, s->user_attrs);
}

int wmain(int argc, wchar_t **argv) {
    HANDLE query_only = GetCurrentProcessToken(), owned = NULL, ipc;
    SNAPSHOT before, after; TOKEN_STATISTICS owned_statistics; LOW_LABEL low = {0};
    TOKEN_MANDATORY_POLICY off = {0}; CALL *call;
    BOOL baseline, identity, open_raw = FALSE, set_raw = FALSE, set_attempted = FALSE, denied = FALSE;
    DWORD open_error = 0, set_error = 0, payload_size, written; TOKEN_INFORMATION_CLASS information;
    void *payload; unsigned mode, index; wchar_t *end; unsigned long long number;
    BYTE header[4];
    if (argc != 3 || argv[1][0] < L'0' || argv[1][0] > L'9') return 90;
    errno = 0; number = _wcstoui64(argv[1], &end, 10);
    if (errno || *end || !number || number > UINTPTR_MAX) return 90;
    ipc = (HANDLE)(uintptr_t)number;
    if (!real_handle(ipc)) return 90;
    mode = !wcscmp(argv[2], L"raise-il") ? 1 : !wcscmp(argv[2], L"disable-no-write-up") ? 2 : 0;
    if (!mode) return 90;
    baseline = capture(query_only, &before, 1) && before.il_known && before.il == 0
        && before.policy_known && (before.policy & TOKEN_MANDATORY_POLICY_NO_WRITE_UP);
    if (!baseline) problems |= 512;
    if (baseline && (call = entered(PROCESS_OPEN, TOKEN_QUERY | TOKEN_ADJUST_DEFAULT)) != NULL) {
        SetLastError(0); open_raw = OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_ADJUST_DEFAULT, &owned);
        open_error = GetLastError();
        call->raw = open_raw; call->error = open_error; call->length = owned != NULL; call->state = 2;
        if (open_raw && real_handle(owned) && query(owned, TokenStatistics, sizeof(owned_statistics), sizeof(owned_statistics))) {
            memcpy(&owned_statistics, scratch, sizeof(owned_statistics));
            if (owned_statistics.TokenType != TokenPrimary || owned_statistics.TokenId.LowPart != before.id.LowPart
                    || owned_statistics.TokenId.HighPart != before.id.HighPart) problems |= 64;
            else {
                static const BYTE low_sid[12] = {1,1,0,0,0,0,0,16,0,16,0,0};
                low.label.Label.Sid = (PSID)low.sid; low.label.Label.Attributes = SE_GROUP_INTEGRITY;
                memcpy(low.sid, low_sid, sizeof(low_sid));
                information = mode == 1 ? TokenIntegrityLevel : TokenMandatoryPolicy;
                payload = mode == 1 ? (void *)&low : (void *)&off;
                payload_size = mode == 1 ? 28 : 4;
                if ((call = entered(SET_TOKEN, (unsigned)information)) != NULL) {
                    set_attempted = TRUE;
                    SetLastError(0); set_raw = SetTokenInformation(owned, information, payload, payload_size);
                    set_error = GetLastError();
                    if (set_raw) attack_succeeded = TRUE; /* Permanent, before subsequent queries/close. */
                    call->raw = set_raw; call->error = set_error; call->length = payload_size; call->state = 2;
                    denied = !set_raw && (set_error == ERROR_ACCESS_DENIED || set_error == ERROR_PRIVILEGE_NOT_HELD);
                    if (!set_raw && !denied) problems |= 256;
                }
            }
        } else if (!open_raw && open_error == ERROR_ACCESS_DENIED && !owned) denied = TRUE;
        else problems |= 128;
    }
    capture(query_only, &after, 2);
    if ((before.il_known && after.il_known && after.il > before.il)
            || (before.policy_known && (before.policy & 1) && after.policy_known && !(after.policy & 1))) attack_succeeded = TRUE;
    identity = same_identity(&before, &after);
    if (!identity) problems |= 1024;
    close_once(&owned, 3);
    append("{\"probe\":\"own_token_v4\",\"mode\":\"%s\",\"attack_succeeded\":%d,\"resistance_observed\":%d,"
        "\"unknown\":%d,\"problems\":%u,\"identity_same\":%d,\"cleanup_ok\":%d,\"open\":[%d,%lu],"
        "\"set\":[%d,%d,%lu],\"before\":", mode == 1 ? "raise-il" : "disable-no-write-up", attack_succeeded,
        !attack_succeeded && !problems && baseline && identity && denied && cleanup_ok && before.il == after.il && before.policy == after.policy,
        !attack_succeeded && (problems || !denied || !cleanup_ok || before.il != after.il || before.policy != after.policy),
        problems, identity, cleanup_ok, open_raw, open_error, set_attempted, set_raw, set_error);
    snapshot_json(&before); append(",\"after\":"); snapshot_json(&after);
    append(",\"refused_handle_count\":%u,\"set_info\":%u,\"set_bytes\":%u,"
        "\"call_budget\":48,\"call_count_before_ipc\":%u,\"maximum_total_calls\":%u,\"calls\":[",
        refused_handles, mode == 1 ? 25 : 27, mode == 1 ? 28 : 4, call_count, call_count + 2);
    for (index = 0; index < call_count; ++index) {
        CALL *c = &calls[index]; append("%s[%u,%u,%u,%ld,%lu,%lu]", index ? "," : "", c->api, c->info, c->state, c->raw, c->error, c->length);
    }
    append("],\"ipc_write_calls_planned\":2}");
    if (!frame_ok || used > FRAME_BUDGET || call_count + 2 > CALL_BUDGET) return 91;
    header[0] = (BYTE)(used >> 24); header[1] = (BYTE)(used >> 16); header[2] = (BYTE)(used >> 8); header[3] = (BYTE)used;
    entered(IPC_WRITE, 1);
    if (!WriteFile(ipc, header, 4, &written, NULL) || written != 4) return 92;
    entered(IPC_WRITE, 2);
    if (!WriteFile(ipc, frame, (DWORD)used, &written, NULL) || written != used) return 93;
    return attack_succeeded ? 10 : problems || !denied || !cleanup_ok || before.il != after.il || before.policy != after.policy ? 2 : 0;
}
