#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif
#if _WIN32_WINNT < 0x0602
#error Mock requires the same Windows 8 declarations as the fixture
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

/* Include SDK/CRT declarations BEFORE macro substitution. Every allowlisted
 * fixture API below is fake; unsupported handles/classes/extra calls abort.
 * Frozen included source SHA256:
 * 42836e8c603344739dc31a0602a1f371b31c1eba0ecdcfc6c6b95054bc1aac6c.
 * The host harness uses CRT stdout only. It creates no Windows objects.
 */
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "mock assertion line %u\n", (unsigned)__LINE__); exit(97); } } while (0)
static HANDLE WINAPI h_process(void), h_thread(void), h_process_token(void);
static VOID WINAPI h_set_error(DWORD error);
static DWORD WINAPI h_get_error(void);
static BOOL WINAPI h_query(HANDLE, TOKEN_INFORMATION_CLASS, LPVOID, DWORD, PDWORD);
static BOOL WINAPI h_open_thread(HANDLE, DWORD, BOOL, PHANDLE);
static BOOL WINAPI h_open_process(HANDLE, DWORD, PHANDLE);
static BOOL WINAPI h_set_token(HANDLE, TOKEN_INFORMATION_CLASS, LPVOID, DWORD);
static BOOL WINAPI h_close(HANDLE);
static BOOL WINAPI h_write(HANDLE, LPCVOID, DWORD, LPDWORD, LPOVERLAPPED);
#undef GetCurrentProcessToken
#undef GetCurrentProcess
#undef GetCurrentThread
#define GetCurrentProcessToken h_process_token
#define GetCurrentProcess h_process
#define GetCurrentThread h_thread
#define SetLastError h_set_error
#define GetLastError h_get_error
#define GetTokenInformation h_query
#define OpenThreadToken h_open_thread
#define OpenProcessToken h_open_process
#define SetTokenInformation h_set_token
#define CloseHandle h_close
#define WriteFile h_write
/* Explicit tripwires for prohibited expansion, in addition to the source pin. */
#define H_FORBIDDEN() (fprintf(stderr, "forbidden fixture API\n"), exit(98), 0)
#define OpenProcess(...) ((HANDLE)(uintptr_t)H_FORBIDDEN())
#define CreateProcessW(...) H_FORBIDDEN()
#define CreateFileW(...) ((HANDLE)(uintptr_t)H_FORBIDDEN())
#define RegOpenKeyExW(...) H_FORBIDDEN()
#define RegSetKeySecurity(...) H_FORBIDDEN()
#define AdjustTokenPrivileges(...) H_FORBIDDEN()
#define ImpersonateLoggedOnUser(...) H_FORBIDDEN()
#define DuplicateTokenEx(...) H_FORBIDDEN()
#define wmain included_fixture_wmain
#include "native_integrity_probe_v4.c"
#undef wmain

enum { H_OPEN5, H_SET5, H_SET1314, H_TRUE, H_VISIBLE_IL, H_VISIBLE_POLICY,
       H_AFTER_READ, H_BAD_POINTER, H_BAD_COUNT, H_BAD_LENGTH, H_THREAD,
       H_PARTIAL_OPEN, H_TOKEN_ID, H_CLOSE, H_UNKNOWN_SET };
typedef struct { const char *name; const wchar_t *mode; unsigned kind;
    BOOL attack, resistance, unknown; unsigned opens, sets, closes, thread_closes; } CASE;
static const CASE cases[] = {
    {"open5-il",L"raise-il",H_OPEN5,0,1,0,1,0,0,0},
    {"open5-policy",L"disable-no-write-up",H_OPEN5,0,1,0,1,0,0,0},
    {"set5-il",L"raise-il",H_SET5,0,1,0,1,1,1,0},
    {"set5-policy",L"disable-no-write-up",H_SET5,0,1,0,1,1,1,0},
    {"set1314-il",L"raise-il",H_SET1314,0,1,0,1,1,1,0},
    {"set1314-policy",L"disable-no-write-up",H_SET1314,0,1,0,1,1,1,0},
    {"success-unchanged-il",L"raise-il",H_TRUE,1,0,0,1,1,1,0},
    {"success-unchanged-policy",L"disable-no-write-up",H_TRUE,1,0,0,1,1,1,0},
    {"false-visible-il",L"raise-il",H_VISIBLE_IL,1,0,0,1,1,1,0},
    {"false-visible-policy",L"disable-no-write-up",H_VISIBLE_POLICY,1,0,0,1,1,1,0},
    {"success-later-read-failed",L"raise-il",H_AFTER_READ,1,0,0,1,1,1,0},
    {"sid-pointer",L"raise-il",H_BAD_POINTER,0,0,1,0,0,0,0},
    {"sid-count",L"raise-il",H_BAD_COUNT,0,0,1,0,0,0,0},
    {"query-length",L"raise-il",H_BAD_LENGTH,0,0,1,0,0,0,0},
    {"unexpected-thread",L"raise-il",H_THREAD,0,0,1,0,0,0,1},
    {"partial-open",L"raise-il",H_PARTIAL_OPEN,0,0,1,1,0,1,0},
    {"owned-token-id",L"raise-il",H_TOKEN_ID,0,0,1,1,0,1,0},
    {"close-failed",L"disable-no-write-up",H_CLOSE,0,0,1,1,1,1,0},
    {"unknown-set87",L"disable-no-write-up",H_UNKNOWN_SET,0,0,1,1,1,1,0}
};
static const CASE *h_case;
static DWORD h_error;
static unsigned h_snapshots, h_opens, h_sets, h_closes, h_thread_opens, h_thread_closes, h_writes, h_bytes;
static unsigned h_queries[3][64];
static BOOL h_owned_live, h_thread_live;
static BYTE h_ipc[4101];

static HANDLE WINAPI h_process(void) { return (HANDLE)(LONG_PTR)-1; }
static HANDLE WINAPI h_thread(void) { return (HANDLE)(LONG_PTR)-2; }
static HANDLE WINAPI h_process_token(void) { return (HANDLE)(LONG_PTR)-4; }
static VOID WINAPI h_set_error(DWORD error) { h_error = error; }
static DWORD WINAPI h_get_error(void) { return h_error; }

static void h_sid(BYTE *out, BYTE authority, BYTE count, const DWORD *parts) {
    memset(out, 0, 8 + 4 * count); out[0] = 1; out[1] = count; out[7] = authority;
    memcpy(out + 8, parts, 4 * count);
}

static BOOL WINAPI h_query(HANDLE token, TOKEN_INFORMATION_CLASS info, LPVOID output, DWORD capacity, PDWORD length) {
    BYTE *buffer = (BYTE *)output; DWORD zero = 0, one = 1, size = 4, rid;
    DWORD user_parts[] = {21,1,2,3,1001}, package_parts[] = {2,1,2,3,4,5,6,7};
    BOOL owned = token == (HANDLE)(uintptr_t)50; unsigned phase;
    CHECK(output == scratch && capacity == sizeof(scratch) && length != NULL);
    CHECK(owned || token == (HANDLE)(LONG_PTR)-4);
    if (owned) CHECK(info == TokenStatistics && h_owned_live);
    else if (info == TokenType) ++h_snapshots;
    phase = owned ? 0 : h_snapshots;
    CHECK(phase <= 2 && (owned || phase >= 1) && (unsigned)info < 64);
    CHECK(++h_queries[phase][info] == 1);
    memset(buffer, 0, capacity);
    if (h_case->kind == H_AFTER_READ && phase == 2 && info == TokenUser) {
        *length = 0; h_error = 5; return FALSE;
    }
    switch (info) {
    case TokenType: memcpy(buffer, &one, 4); break;
    case TokenUser: {
        TOKEN_USER value = {0}; value.User.Sid = (PSID)(buffer + sizeof(value));
        h_sid(buffer + sizeof(value), 5, 5, user_parts); size = (DWORD)sizeof(value) + 28;
        if (phase == 1 && h_case->kind == H_BAD_POINTER) value.User.Sid = (PSID)(buffer + capacity - 4);
        if (phase == 1 && h_case->kind == H_BAD_COUNT) buffer[sizeof(value) + 1] = 16;
        memcpy(buffer, &value, sizeof(value));
        if (phase == 1 && h_case->kind == H_BAD_LENGTH) size = (DWORD)sizeof(value) - 1;
        break;
    }
    case TokenAppContainerSid: {
        PSID sid = (PSID)(buffer + sizeof(sid)); memcpy(buffer, &sid, sizeof(sid));
        h_sid(buffer + sizeof(sid), 15, 8, package_parts); size = (DWORD)sizeof(sid) + 40; break;
    }
    case TokenStatistics: {
        TOKEN_STATISTICS value = {0}; value.TokenType = TokenPrimary;
        value.TokenId.LowPart = owned && h_case->kind == H_TOKEN_ID ? 102 : 101;
        memcpy(buffer, &value, sizeof(value)); size = (DWORD)sizeof(value); break;
    }
    case TokenIsAppContainer: memcpy(buffer, &one, 4); break;
    case TokenCapabilities: memcpy(buffer, &zero, 4); break;
    case TokenIntegrityLevel: {
        TOKEN_MANDATORY_LABEL value = {0}; value.Label.Sid = (PSID)(buffer + sizeof(value));
        value.Label.Attributes = 0x60; rid = phase == 2 && h_case->kind == H_VISIBLE_IL ? 4096 : 0;
        h_sid(buffer + sizeof(value), 16, 1, &rid); memcpy(buffer, &value, sizeof(value)); size = (DWORD)sizeof(value) + 12; break;
    }
    case TokenMandatoryPolicy: memcpy(buffer, phase == 2 && h_case->kind == H_VISIBLE_POLICY ? &zero : &one, 4); break;
    default: CHECK(FALSE); return FALSE;
    }
    CHECK(size <= capacity); *length = size; return TRUE;
}

static BOOL WINAPI h_open_thread(HANDLE thread, DWORD access, BOOL as_self, PHANDLE output) {
    CHECK(thread == (HANDLE)(LONG_PTR)-2 && access == TOKEN_QUERY && as_self && output && !*output);
    CHECK(h_snapshots <= 1 && ++h_thread_opens == h_snapshots + 1);
    if (h_case->kind == H_THREAD && h_snapshots == 0) {
        CHECK(!h_thread_live); h_thread_live = TRUE; *output = (HANDLE)(uintptr_t)60; return TRUE;
    }
    h_error = ERROR_NO_TOKEN; return FALSE;
}

static BOOL WINAPI h_open_process(HANDLE process, DWORD access, PHANDLE output) {
    CHECK(process == (HANDLE)(LONG_PTR)-1 && access == 0x88 && output && !*output && ++h_opens == 1);
    if (h_case->kind == H_OPEN5) { h_error = 5; return FALSE; }
    h_owned_live = TRUE; *output = (HANDLE)(uintptr_t)50;
    if (h_case->kind == H_PARTIAL_OPEN) { h_error = 5; return FALSE; }
    return TRUE;
}

static BOOL WINAPI h_set_token(HANDLE token, TOKEN_INFORMATION_CLASS info, LPVOID input, DWORD size) {
    static const BYTE sid[12] = {1,1,0,0,0,0,0,16,0,16,0,0}; DWORD policy;
    CHECK(token == (HANDLE)(uintptr_t)50 && h_owned_live && h_opens == 1 && ++h_sets == 1);
    if (!wcscmp(h_case->mode, L"raise-il")) {
        TOKEN_MANDATORY_LABEL *label = (TOKEN_MANDATORY_LABEL *)input;
        CHECK(info == TokenIntegrityLevel && size == 28 && label->Label.Attributes == 0x20);
        CHECK(label->Label.Sid == (BYTE *)input + 16 && !memcmp(label->Label.Sid, sid, sizeof(sid)));
    } else { CHECK(info == TokenMandatoryPolicy && size == 4); memcpy(&policy, input, 4); CHECK(policy == 0); }
    if (h_case->kind == H_TRUE || h_case->kind == H_AFTER_READ) return TRUE;
    h_error = h_case->kind == H_SET1314 ? 1314 : h_case->kind == H_UNKNOWN_SET ? 87 : 5;
    return FALSE;
}

static BOOL WINAPI h_close(HANDLE token) {
    if (token == (HANDLE)(uintptr_t)50) {
        CHECK(h_owned_live && ++h_closes == 1);
        if (h_case->kind == H_CLOSE) { h_error = 5; return FALSE; }
        h_owned_live = FALSE;
    } else { CHECK(token == (HANDLE)(uintptr_t)60 && h_thread_live && ++h_thread_closes == 1); h_thread_live = FALSE; }
    return TRUE;
}

static BOOL WINAPI h_write(HANDLE handle, LPCVOID data, DWORD size, LPDWORD written, LPOVERLAPPED overlap) {
    CHECK(handle == (HANDLE)(uintptr_t)42 && !overlap && written && h_writes < 2);
    CHECK(size == 4 || h_writes == 1); CHECK(h_writes != 0 || size == 4);
    CHECK(h_bytes + size < sizeof(h_ipc)); memcpy(h_ipc + h_bytes, data, size);
    h_bytes += size; ++h_writes; *written = size; return TRUE;
}

static void h_reset(const CASE *selected) {
    h_case = selected; h_error = 0; h_snapshots = h_opens = h_sets = h_closes = h_thread_opens = h_thread_closes = h_writes = h_bytes = 0;
    h_owned_live = h_thread_live = FALSE; memset(h_queries, 0, sizeof(h_queries)); memset(h_ipc, 0, sizeof(h_ipc));
    memset(calls, 0, sizeof(calls)); memset(scratch, 0, sizeof(scratch)); memset(frame, 0, sizeof(frame));
    call_count = problems = refused_handles = 0; received = 0; attack_succeeded = FALSE; cleanup_ok = TRUE;
    used = 0; frame_ok = TRUE;
}

int main(void) {
    size_t index; unsigned body_size; int code;
    wchar_t program[] = L"fixed-mock", ipc[] = L"42", mode[32]; wchar_t *arguments[] = {program, ipc, mode, NULL};
    for (index = 0; index < sizeof(cases) / sizeof(cases[0]); ++index) {
        const CASE *selected = &cases[index]; h_reset(selected); CHECK(wcslen(selected->mode) < 32);
        wcscpy_s(mode, 32, selected->mode); code = included_fixture_wmain(3, arguments);
        CHECK(h_writes == 2 && h_bytes >= 4); body_size = ((unsigned)h_ipc[0] << 24) | ((unsigned)h_ipc[1] << 16) | ((unsigned)h_ipc[2] << 8) | h_ipc[3];
        CHECK(body_size == h_bytes - 4 && body_size <= FRAME_BUDGET && call_count <= CALL_BUDGET);
        CHECK(attack_succeeded == selected->attack && code == (selected->attack ? 10 : selected->unknown ? 2 : 0));
        CHECK((strstr((char *)h_ipc + 4, "\"resistance_observed\":1") != NULL) == selected->resistance);
        CHECK((strstr((char *)h_ipc + 4, "\"unknown\":1") != NULL) == selected->unknown);
        CHECK(h_opens == selected->opens && h_sets == selected->sets && h_closes == selected->closes && h_thread_opens == 2 && h_thread_closes == selected->thread_closes);
        printf("{\"case\":\"%s\",\"exit_code\":%d,\"open_calls\":%u,\"set_calls\":%u,\"owned_close_calls\":%u,\"thread_close_calls\":%u,\"remaining_owned\":%d,\"frame\":%s}\n",
            selected->name, code, h_opens, h_sets, h_closes, h_thread_closes, h_owned_live, (char *)h_ipc + 4);
    }
    return 0;
}
