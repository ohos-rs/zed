#include <unistd.h>
static uid_t real_user_id(void) { return getuid(); }
static gid_t real_group_id(void) { return getgid(); }
#define getuid test_getuid
#define geteuid test_geteuid
#define getgid test_getgid
#define getegid test_getegid
uid_t test_getuid(void);
uid_t test_geteuid(void);
gid_t test_getgid(void);
gid_t test_getegid(void);
#include "openssh_ohos.c"
#include <assert.h>

static uid_t current_user, effective_user;
static gid_t current_group, effective_group;
static struct passwd *system_account;
static int lookup_error;

uid_t test_getuid(void) { return current_user; }
uid_t test_geteuid(void) { return effective_user; }
gid_t test_getgid(void) { return current_group; }
gid_t test_getegid(void) { return effective_group; }

struct passwd *__real_getpwuid(uid_t user_id) {
    errno = lookup_error;
    return system_account != NULL && system_account->pw_uid == user_id ? system_account : NULL;
}

struct passwd *__real_getpwnam(const char *name) {
    errno = lookup_error;
    return system_account != NULL && strcmp(system_account->pw_name, name) == 0 ? system_account : NULL;
}

int main(void) {
    current_user = effective_user = real_user_id();
    current_group = effective_group = real_group_id();
    char home[] = "/tmp/zed-openssh-home-XXXXXX";
    assert(mkdtemp(home) != NULL);
    assert(setenv("HOME", home, 1) == 0);
    assert(setenv("USER", "100", 1) == 0);
    assert(setenv("SHELL", "/system/bin/sh", 1) == 0);
    struct passwd *account = __wrap_getpwuid(getuid());
    assert(account != NULL && account->pw_uid == getuid() && account->pw_gid == getgid());
    assert(strcmp(account->pw_dir, home) == 0);
    assert(strcmp(account->pw_shell, "/system/bin/sh") == 0);
    assert(__wrap_getpwnam("100") == account);
    assert(__wrap_getpwuid(42) == NULL && __wrap_getpwnam("someone_else") == NULL);
    current_user = effective_user = real_user_id() + 1;
    account = __wrap_getpwuid(getuid());
    assert(account != NULL && strcmp(account->pw_dir, "/data/storage/el2/base/files") == 0);
    current_user = effective_user = real_user_id();

    struct passwd existing = {.pw_uid = getuid(), .pw_name = "real_account", .pw_dir = "/real/home"};
    system_account = &existing;
    assert(__wrap_getpwuid(getuid()) == &existing);
    assert(__wrap_getpwnam("real_account") == &existing);
    assert(__wrap_getpwnam("100") == NULL);
    system_account = NULL;

    lookup_error = EIO;
    assert(__wrap_getpwuid(getuid()) == NULL && errno == EIO);
    assert(__wrap_getpwnam("100") == NULL && errno == EIO);
    lookup_error = 0;
    effective_user = current_user + 1;
    assert(__wrap_getpwuid(getuid()) == NULL);
    effective_user = getuid();
    effective_group = current_group + 1;
    assert(__wrap_getpwuid(getuid()) == NULL);
    effective_group = getgid();

    assert(unsetenv("HOME") == 0);
    assert(__wrap_getpwuid(getuid()) == NULL);
    assert(setenv("HOME", "relative", 1) == 0);
    assert(__wrap_getpwuid(getuid()) == NULL);
    char missing[256];
    assert(snprintf(missing, sizeof(missing), "%s/missing", home) > 0);
    assert(setenv("HOME", missing, 1) == 0);
    assert(__wrap_getpwuid(getuid()) == NULL && errno == ENOENT);
    assert(setenv("HOME", home, 1) == 0);
    assert(unsetenv("USER") == 0 && unsetenv("SHELL") == 0);
    account = __wrap_getpwuid(getuid());
    char generated_name[64];
    assert(snprintf(generated_name, sizeof(generated_name), "app_%lu", (unsigned long)getuid()) > 0);
    assert(account != NULL && strcmp(account->pw_name, generated_name) == 0);
    assert(strcmp(account->pw_shell, "/system/bin/sh") == 0);
    int inherited[2], runtime[2];
    assert(pipe(inherited) == 0 && pipe(runtime) == 0);
    assert(fcntl(runtime[0], F_SETFD, FD_CLOEXEC) == 0);
    assert(fcntl(runtime[1], F_SETFD, FD_CLOEXEC) == 0);
    assert(ohos_close_inherited_descriptors(3) == 0);
    assert(fcntl(inherited[0], F_GETFD) == -1 && errno == EBADF);
    assert(fcntl(inherited[1], F_GETFD) == -1 && errno == EBADF);
    assert(fcntl(runtime[0], F_GETFD) == FD_CLOEXEC);
    assert(fcntl(runtime[1], F_GETFD) == FD_CLOEXEC);
    assert(close(runtime[0]) == 0 && close(runtime[1]) == 0);
    assert(rmdir(home) == 0);
    puts("OpenSSH OHOS account and descriptor compatibility tests passed");
    return 0;
}
