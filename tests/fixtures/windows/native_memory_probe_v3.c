#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

/* A distinct bounded commit fixture, never a replacement for native_probe_v2.
 * No command-line allocation count, size, quota, or alternate mode exists.
 * These are commit requests, not a physical-memory/page-touch stress test.
 */
#define ALLOCATION_BYTES (16UL * 1024UL * 1024UL)
#define MAX_ALLOCATION_COUNT 32UL
#define ALLOCATION_CEILING_BYTES (ALLOCATION_BYTES * MAX_ALLOCATION_COUNT)

static int emit(HANDLE pipe, const char *body) {
    DWORD size = (DWORD)strlen(body), written = 0;
    unsigned char header[4] = {(unsigned char)(size >> 24), (unsigned char)(size >> 16),
                               (unsigned char)(size >> 8), (unsigned char)size};
    if (!WriteFile(pipe, header, 4, &written, NULL) || written != 4) return 91;
    if (!WriteFile(pipe, body, size, &written, NULL) || written != size) return 92;
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    HANDLE pipe, excluded;
    LPVOID allocations[MAX_ALLOCATION_COUNT] = {0};
    DWORD flags = 0, excluded_error = 0, excluded_exception = 0;
    DWORD allocation_count = 0, allocation_error = 0, freed_count = 0, free_error = 0;
    DWORD index;
    BOOL inherited = FALSE, bounded_memory, free_failed = FALSE;
    int emitted;
    char body[2048];

    if (argc != 4) return 90;
    if (wcscmp(argv[3], L"smoke") == 0) bounded_memory = FALSE;
    else if (wcscmp(argv[3], L"bounded-memory") == 0) bounded_memory = TRUE;
    else return 90;
    pipe = (HANDLE)(uintptr_t)_wcstoui64(argv[1], NULL, 10);
    excluded = (HANDLE)(uintptr_t)_wcstoui64(argv[2], NULL, 10);
    SetLastError(0);
    __try {
        inherited = GetHandleInformation(excluded, &flags);
    } __except (GetExceptionCode() == 0xC0000008UL
                    ? EXCEPTION_EXECUTE_HANDLER : EXCEPTION_CONTINUE_SEARCH) {
        excluded_exception = GetExceptionCode();
    }
    if (excluded_exception == 0) excluded_error = GetLastError();

    if (bounded_memory) {
        while (allocation_count < MAX_ALLOCATION_COUNT) {
            LPVOID block = VirtualAlloc(NULL, (SIZE_T)ALLOCATION_BYTES,
                                       MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
            if (block == NULL) {
                allocation_error = GetLastError();
                break;
            }
            allocations[allocation_count] = block;
            ++allocation_count;
        }
    }
    /* Release every successful allocation in reverse order BEFORE formatting
     * or emitting. Even a formatting/pipe failure cannot skip this cleanup.
     * Attempt all blocks after an error and preserve the first exact error.
     */
    for (index = allocation_count; index > 0; --index) {
        if (VirtualFree(allocations[index - 1], 0, MEM_RELEASE)) {
            allocations[index - 1] = NULL;
            ++freed_count;
        } else {
            DWORD observed_error = GetLastError();
            if (!free_failed) free_error = observed_error;
            free_failed = TRUE;
        }
    }

    if (sprintf_s(body, sizeof(body),
        "{\"probe\":\"native_memory_v3\",\"fixture_version\":3,\"mode\":\"%s\","
        "\"excluded_handle_valid\":%s,\"excluded_handle_error\":%lu,\"excluded_handle_exception\":%lu,"
        "\"allocation_attempted\":%s,\"allocation_count\":%lu,\"allocation_bytes\":%lu,"
        "\"allocation_unit_bytes\":%lu,\"allocation_ceiling_bytes\":%lu,\"allocation_error\":%lu,"
        "\"allocation_ceiling_reached\":%s,\"freed_count\":%lu,\"freed_bytes\":%lu,"
        "\"free_error\":%lu,\"all_allocations_freed\":%s}",
        bounded_memory ? "bounded-memory" : "smoke", inherited ? "true" : "false",
        excluded_error, excluded_exception, bounded_memory ? "true" : "false",
        allocation_count, allocation_count * ALLOCATION_BYTES, ALLOCATION_BYTES,
        ALLOCATION_CEILING_BYTES, allocation_error,
        allocation_count == MAX_ALLOCATION_COUNT ? "true" : "false",
        freed_count, freed_count * ALLOCATION_BYTES, free_error,
        freed_count == allocation_count ? "true" : "false") < 0) return 98;
    emitted = emit(pipe, body);
    if (emitted != 0) return emitted;
    return freed_count == allocation_count ? 0 : 99;
}
