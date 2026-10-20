#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

struct passwd *__real_getpwuid(uid_t user_id);
struct passwd *__real_getpwnam(const char *name);

static int close_inherited_descriptor(int descriptor) {
    int flags = fcntl(descriptor, F_GETFD);
    if (flags == -1) {
        return errno == EBADF ? 0 : -1;
    }
    /* CLOEXEC descriptors cannot survive exec; libc created them in this process. */
    if (flags & FD_CLOEXEC) {
        return 0;
    }
    return close(descriptor);
}

int ohos_close_inherited_descriptors(int first_descriptor) {
    DIR *directory = opendir("/proc/self/fd");
    if (directory == NULL) {
        long limit = sysconf(_SC_OPEN_MAX);
        if (limit < 0 || limit > INT_MAX) {
            errno = EOVERFLOW;
            return -1;
        }
        for (int descriptor = first_descriptor; descriptor < limit; descriptor++) {
            if (close_inherited_descriptor(descriptor) != 0) {
                return -1;
            }
        }
        return 0;
    }
    int error = 0;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(directory);
        if (entry == NULL) {
            error = errno;
            break;
        }
        char *end;
        long descriptor = strtol(entry->d_name, &end, 10);
        if (end == entry->d_name || *end != '\0' || descriptor < first_descriptor ||
            descriptor > INT_MAX || descriptor == dirfd(directory)) {
            continue;
        }
        if (close_inherited_descriptor((int)descriptor) != 0) {
            error = errno;
            break;
        }
    }
    if (closedir(directory) != 0 && error == 0) {
        error = errno;
    }
    errno = error;
    return error == 0 ? 0 : -1;
}

static struct passwd *application_account(void) {
    static struct passwd account;
    static char generated_name[64];
    char *home = getenv("HOME");
    char *name = getenv("USER");
    char *shell = getenv("SHELL");

    /* App UIDs have no passwd entry. Never synthesize an identity for a privileged executable. */
    if (getuid() != geteuid() || getgid() != getegid() || home == NULL || home[0] != '/') {
        errno = ENOENT;
        return NULL;
    }
    struct stat metadata;
    if (stat(home, &metadata) != 0) {
        return NULL;
    }
    if (!S_ISDIR(metadata.st_mode)) {
        errno = ENOTDIR;
        return NULL;
    }
    if (strcmp(home, "/") == 0 || metadata.st_uid != getuid()) {
        /* Shared document storage remaps modes and ownership; use the app's private home. */
        home = "/data/storage/el2/base/files";
    }
    if (name == NULL || name[0] == '\0') {
        int length = snprintf(generated_name, sizeof(generated_name), "app_%lu",
                              (unsigned long)getuid());
        if (length < 0 || (size_t)length >= sizeof(generated_name)) {
            errno = ERANGE;
            return NULL;
        }
        name = generated_name;
    }
    account = (struct passwd) {
        .pw_name = name,
        .pw_passwd = "*",
        .pw_uid = getuid(),
        .pw_gid = getgid(),
        .pw_gecos = name,
        .pw_dir = home,
        .pw_shell = shell != NULL && shell[0] == '/' ? shell : "/system/bin/sh",
    };
    errno = 0;
    return &account;
}

struct passwd *__wrap_getpwuid(uid_t user_id) {
    errno = 0;
    struct passwd *account = __real_getpwuid(user_id);
    if (account != NULL || user_id != getuid() || (errno != 0 && errno != ENOENT)) {
        return account;
    }
    return application_account();
}

struct passwd *__wrap_getpwnam(const char *name) {
    errno = 0;
    struct passwd *account = __real_getpwnam(name);
    if (account != NULL || (errno != 0 && errno != ENOENT)) {
        return account;
    }
    account = __wrap_getpwuid(getuid());
    if (account == NULL || strcmp(account->pw_name, name) == 0) {
        return account;
    }
    errno = ENOENT;
    return NULL;
}
