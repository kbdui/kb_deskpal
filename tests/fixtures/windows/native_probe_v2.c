#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

/* Synthetic stage-zero fixture v2. Keep strict handle checking enabled.
 * Only the deliberately excluded-handle query may catch STATUS_INVALID_HANDLE;
 * every other structured exception continues to the normal handler search.
 */
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
    DWORD flags = 0, error = 0, denied = 0, allocation_count = 0;
    DWORD excluded_exception = 0;
    BOOL inherited = FALSE;
    char body[1024];
    if (argc != 4) return 90;
    pipe = (HANDLE)(uintptr_t)_wcstoui64(argv[1], NULL, 10);
    excluded = (HANDLE)(uintptr_t)_wcstoui64(argv[2], NULL, 10);
    SetLastError(0);
    __try {
        inherited = GetHandleInformation(excluded, &flags);
    } __except (GetExceptionCode() == 0xC0000008UL
                    ? EXCEPTION_EXECUTE_HANDLER : EXCEPTION_CONTINUE_SEARCH) {
        excluded_exception = GetExceptionCode();
    }
    /* No Win32 return error exists when the API raised instead of returning. */
    if (excluded_exception == 0) error = GetLastError();
    if (wcscmp(argv[3], L"spin") == 0) {
        volatile DWORD counter = 0;
        for (;;) ++counter;
    }
    if (wcscmp(argv[3], L"flood") == 0) {
        char data[4096] = {0};
        DWORD written;
        for (;;) if (!WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), data, sizeof(data),
                                 &written, NULL)) return 93;
    }
    if (wcscmp(argv[3], L"invalid-frame") == 0) {
        DWORD written;
        unsigned char data[4] = {255,255,255,255};
        return WriteFile(pipe, data, 4, &written, NULL) ? 0 : 94;
    }
    if (wcscmp(argv[3], L"memory") == 0) {
        while (allocation_count < 1024) {
            if (!VirtualAlloc(NULL, 16 * 1024 * 1024, MEM_RESERVE | MEM_COMMIT,
                              PAGE_READWRITE)) { denied = GetLastError(); break; }
            ++allocation_count;
        }
    }
    if (wcscmp(argv[3], L"child") == 0) {
        wchar_t application[MAX_PATH], command[MAX_PATH + 4];
        STARTUPINFOW startup = {0};
        PROCESS_INFORMATION child = {0};
        startup.cb = sizeof(startup);
        if (!GetModuleFileNameW(NULL, application, MAX_PATH)) return 95;
        if (swprintf_s(command, MAX_PATH + 4, L"\"%ls\"", application) < 0) return 96;
        if (CreateProcessW(application, command, NULL, NULL, FALSE, CREATE_SUSPENDED,
                           NULL, NULL, &startup, &child)) {
            TerminateProcess(child.hProcess, 97);
            WaitForSingleObject(child.hProcess, 2000);
            CloseHandle(child.hThread); CloseHandle(child.hProcess);
            denied = 0;
        } else denied = GetLastError();
    }
    if (sprintf_s(body, sizeof(body),
        "{\"probe\":\"native\",\"excluded_handle_valid\":%s,\"excluded_handle_error\":%lu,"
        "\"excluded_handle_exception\":%lu,\"operation_error\":%lu,\"allocation_count\":%lu}",
        inherited ? "true" : "false", error, excluded_exception, denied, allocation_count) < 0) return 98;
    return emit(pipe, body);
}
