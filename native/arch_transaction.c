#define _GNU_SOURCE
#include "arch_transaction.h"

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define RECEIPT_HEADER "pennix-arch-v1 "
#define RECEIPT_DONE "committed\n"
#define START_SIZE (sizeof(RECEIPT_HEADER) - 1 + 64 + 1)
#define DONE_SIZE (sizeof(RECEIPT_DONE) - 1)

static bool lowercase_hex(const char *value, size_t len)
{
    return value && strnlen(value, len + 1) == len && strspn(value, "0123456789abcdef") == len;
}

static bool receipt_context(const struct arch_transaction_receipt *receipt)
{
    struct stat st;
    return receipt && lowercase_hex(receipt->id, 32) && lowercase_hex(receipt->request_sha256, 64) &&
           fstat(receipt->dirfd, &st) == 0 && S_ISDIR(st.st_mode) &&
           st.st_uid == geteuid() && !(st.st_mode & 022);
}

static void receipt_header(char out[START_SIZE + 1], const struct arch_transaction_receipt *receipt)
{
    memcpy(out, RECEIPT_HEADER, sizeof(RECEIPT_HEADER) - 1);
    memcpy(out + sizeof(RECEIPT_HEADER) - 1, receipt->request_sha256, 64);
    out[START_SIZE - 1] = '\n';
    out[START_SIZE] = '\0';
}

static bool write_all(int fd, const char *data, size_t size)
{
    while (size) {
        ssize_t n = write(fd, data, size);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return false;
        data += (size_t)n;
        size -= (size_t)n;
    }
    return true;
}

enum arch_transaction_result arch_observe_receipt(const struct arch_transaction_receipt *receipt)
{
    if (!receipt_context(receipt))
        return ARCH_OUTCOME_UNKNOWN;
    int fd = openat(receipt->dirfd, receipt->id, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0)
        return ARCH_OUTCOME_UNKNOWN;
    struct stat st;
    char expected[START_SIZE + 1], actual[START_SIZE + DONE_SIZE];
    receipt_header(expected, receipt);
    bool ok = fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && st.st_uid == geteuid() &&
              !(st.st_mode & 077) && st.st_nlink == 1 && st.st_size == sizeof(actual) &&
              read(fd, actual, sizeof(actual)) == sizeof(actual) &&
              memcmp(actual, expected, START_SIZE) == 0 &&
              memcmp(actual + START_SIZE, RECEIPT_DONE, DONE_SIZE) == 0 &&
              fsync(fd) == 0 && fsync(receipt->dirfd) == 0;
    return close(fd) == 0 && ok ? ARCH_COMMITTED : ARCH_OUTCOME_UNKNOWN;
}

static void decline_question(void *ctx, alpm_question_t *question)
{
    *(bool *)ctx = true;
    /* Index zero would choose a provider, not decline it. Refuse the plan. */
    question->any.answer = 0;
}

static int deny_fetch(void *ctx, const char *url, const char *localpath, int force)
{
    (void)ctx;
    (void)url;
    (void)localpath;
    (void)force;
    return -1;
}

struct commit_observer {
    alpm_cb_event event;
    void *event_ctx;
    alpm_cb_log log;
    void *log_ctx;
    bool done;
    bool error;
};

static void observe_event(void *ctx, alpm_event_t *event)
{
    struct commit_observer *observer = ctx;
    if (event->type == ALPM_EVENT_TRANSACTION_DONE)
        observer->done = true;
    if (observer->event)
        observer->event(observer->event_ctx, event);
}

static void observe_log(void *ctx, alpm_loglevel_t level, const char *format, va_list args)
{
    struct commit_observer *observer = ctx;
    if (level & ALPM_LOG_ERROR)
        observer->error = true;
    if (observer->log)
        observer->log(observer->log_ctx, level, format, args);
}

static bool match(const char *a, const char *b)
{
    return a && b ? strcmp(a, b) == 0 : a == b;
}

static bool private_file(const char *path)
{
    struct stat st;
    return lstat(path, &st) == 0 && S_ISREG(st.st_mode) &&
           st.st_uid == geteuid() && !(st.st_mode & 022) && st.st_nlink == 1;
}

static alpm_pkg_t *load_staged(alpm_handle_t *handle, const char *cache, const char *filename,
                              const char *sha256, const char *name, const char *version, int full)
{
    char *path = NULL, *sig = NULL, *sum = NULL;
    alpm_pkg_t *staged = NULL;
    if (!filename || !*filename || strchr(filename, '/') ||
        !strcmp(filename, ".") || !strcmp(filename, "..") ||
        asprintf(&path, "%s/%s", cache, filename) < 0)
        return NULL;
    if (asprintf(&sig, "%s.sig", path) < 0) {
        free(path);
        return NULL;
    }
    if (private_file(path) && private_file(sig) && (sum = alpm_compute_sha256sum(path)) &&
        match(sum, sha256) && alpm_pkg_load(handle, path, full, ALPM_SIG_PACKAGE, &staged) == 0) {
        if (!match(name, alpm_pkg_get_name(staged)) || !match(version, alpm_pkg_get_version(staged))) {
            alpm_pkg_free(staged);
            staged = NULL;
        }
    }
    free(sum);
    free(sig);
    free(path);
    return staged;
}

static bool matches_installed(alpm_handle_t *handle, alpm_db_t *local, const char *name, const char *expected)
{
    alpm_pkg_t *old = alpm_db_get_pkg(local, name);
    if (!old)
        return alpm_errno(handle) == ALPM_ERR_PKG_NOT_FOUND && !expected;
    const char *installed = alpm_pkg_get_version(old);
    return installed && match(installed, expected);
}

static bool check_baseline(alpm_handle_t *handle, const struct arch_installed_package *installed, size_t count)
{
    alpm_db_t *local = alpm_get_localdb(handle);
    if (!local || alpm_db_get_valid(local) != 0)
        return false;
    const alpm_list_t *packages = alpm_db_get_pkgcache(local);
    if ((!packages && alpm_errno(handle) != ALPM_ERR_OK) || alpm_list_count(packages) != count)
        return false;
    for (size_t i = 0; i < count; ++i) {
        const char *name = installed[i].name, *version = installed[i].version;
        if (!name || !*name || !version || !*version ||
            (i && strcmp(installed[i - 1].name, name) >= 0) ||
            !matches_installed(handle, local, name, version))
            return false;
    }
    return true;
}

static bool is_upgrade(const struct arch_reviewed_package *row)
{
    return row->version && (!row->installed || alpm_pkg_vercmp(row->version, row->installed) > 0);
}

static bool signed_by_local_owner(alpm_pkg_t *pkg, const char *fingerprint)
{
    alpm_siglist_t signatures = {0};
    bool known = alpm_pkg_check_pgp_signature(pkg, &signatures) == 0 &&
                 signatures.count == 1 && signatures.results &&
                 signatures.results[0].status == ALPM_SIGSTATUS_VALID &&
                 signatures.results[0].validity == ALPM_SIGVALIDITY_FULL &&
                 !signatures.results[0].key.revoked &&
                 match(signatures.results[0].key.fingerprint, fingerprint);
    return alpm_siglist_cleanup(&signatures) == 0 && known;
}

static bool add_local(alpm_handle_t *handle, const char *cache, const struct arch_reviewed_package *row,
                      const char *fingerprint)
{
    alpm_db_t *local = alpm_get_localdb(handle);
    if (!fingerprint || !*fingerprint || !local || !row->name || !row->sha256 ||
        !matches_installed(handle, local, row->name, row->installed) ||
        !is_upgrade(row) ||
        alpm_pkg_find(alpm_trans_get_add(handle), row->name))
        return false;
    for (const alpm_list_t *db = alpm_get_syncdbs(handle); db; db = db->next) {
        if (alpm_db_get_pkg(db->data, row->name) || alpm_errno(handle) != ALPM_ERR_PKG_NOT_FOUND)
            return false; /* Do not let a local artifact replace a sync candidate. */
    }
    alpm_pkg_t *pkg = load_staged(handle, cache, row->filename, row->sha256, row->name, row->version, 1);
    if (!pkg)
        return false;
    if (!signed_by_local_owner(pkg, fingerprint) || alpm_pkg_should_ignore(handle, pkg) ||
        alpm_pkg_get_replaces(pkg) || alpm_add_pkg(handle, pkg) != 0) {
        alpm_pkg_free(pkg);
        return false;
    }
    return true; /* Transaction release now owns the file package. */
}

static bool check_additions(alpm_handle_t *handle, const struct arch_reviewed_package *expected,
                            size_t count, const char *cache)
{
    alpm_db_t *local = alpm_get_localdb(handle);
    const alpm_list_t *add = alpm_trans_get_add(handle);
    if (!local || alpm_trans_get_remove(handle) || alpm_list_count(add) != count)
        return false;

    for (; add; add = add->next) {
        alpm_pkg_t *pkg = add->data;
        alpm_db_t *db = alpm_pkg_get_db(pkg);
        const char *name = alpm_pkg_get_name(pkg);
        if (!name)
            return false;
        size_t i;
        for (i = 0; i < count; ++i)
            if (match(name, expected[i].name))
                break;
        if (i == count || !is_upgrade(&expected[i]) || !expected[i].sha256 ||
            !matches_installed(handle, local, name, expected[i].installed) ||
            !match(alpm_pkg_get_version(pkg), expected[i].version))
            return false;
        if (!expected[i].repo) {
            if (alpm_pkg_get_origin(pkg) != ALPM_PKG_FROM_FILE)
                return false;
            continue; /* Only add_local can put a file package in this transaction. */
        }
        if (!db || alpm_pkg_get_origin(pkg) != ALPM_PKG_FROM_SYNCDB ||
            !match(alpm_db_get_name(db), expected[i].repo) ||
            !match(alpm_pkg_get_sha256sum(pkg), expected[i].sha256))
            return false;
        int siglevel = alpm_db_get_siglevel(db);
        if (siglevel < 0 || !(siglevel & ALPM_SIG_PACKAGE) ||
            siglevel & (ALPM_SIG_PACKAGE_OPTIONAL | ALPM_SIG_PACKAGE_MARGINAL_OK |
                        ALPM_SIG_PACKAGE_UNKNOWN_OK))
            return false;
        const char *filename = alpm_pkg_get_filename(pkg);
        if (expected[i].filename && !match(filename, expected[i].filename))
            return false;
        alpm_pkg_t *staged = load_staged(handle, cache, filename, expected[i].sha256,
                                         name, expected[i].version, 0);
        if (!staged)
            return false;
        alpm_pkg_free(staged);
    }
    return true;
}

static void free_details(alpm_list_t *details, alpm_errno_t error)
{
    for (alpm_list_t *p = details; p; p = p->next) {
        switch (error) {
        case ALPM_ERR_UNSATISFIED_DEPS: alpm_depmissing_free(p->data); break;
        case ALPM_ERR_CONFLICTING_DEPS: alpm_conflict_free(p->data); break;
        case ALPM_ERR_FILE_CONFLICTS: alpm_fileconflict_free(p->data); break;
        case ALPM_ERR_PKG_INVALID_ARCH:
        case ALPM_ERR_PKG_INVALID:
        case ALPM_ERR_PKG_INVALID_CHECKSUM:
        case ALPM_ERR_PKG_INVALID_SIG: free(p->data); break;
        default: break;
        }
    }
    alpm_list_free(details);
}

enum arch_transaction_result arch_commit_reviewed(
    alpm_handle_t *handle, const char *const *targets, size_t target_count,
    const struct arch_reviewed_package *expected, size_t expected_count,
    const struct arch_installed_package *installed, size_t installed_count,
    const char *local_signer, const struct arch_transaction_receipt *receipt,
    bool (*policy_current)(void *), void *policy_context)
{
    if (!handle || !expected || !expected_count || (target_count && !targets) ||
        (installed_count && !installed))
        return ARCH_REFUSED;
    struct stat st;
    if (!receipt_context(receipt))
        return ARCH_REFUSED;
    if (fstatat(receipt->dirfd, receipt->id, &st, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT)
        return ARCH_OUTCOME_UNKNOWN; /* An existing attempt must be observed, not replayed. */
    const alpm_list_t *caches = alpm_option_get_cachedirs(handle);
    const char *cache = caches ? caches->data : NULL;
    if (!cache || caches->next)
        return ARCH_REFUSED;
    char *directory = strdup(cache);
    if (!directory)
        return ARCH_REFUSED;
    for (size_t n = strlen(directory); n && directory[n - 1] == '/'; --n)
        directory[n - 1] = '\0';
    bool private_cache = lstat(directory, &st) == 0 && S_ISDIR(st.st_mode) &&
                         st.st_uid == geteuid() && !(st.st_mode & 077);
    free(directory);
    if (!private_cache)
        return ARCH_REFUSED;
    if (alpm_trans_init(handle, ALPM_TRANS_FLAG_NEEDED) != 0)
        return ARCH_REFUSED;

    bool question = false;
    alpm_cb_question previous = alpm_option_get_questioncb(handle);
    void *previous_ctx = alpm_option_get_questioncb_ctx(handle);
    alpm_cb_fetch previous_fetch = alpm_option_get_fetchcb(handle);
    void *previous_fetch_ctx = alpm_option_get_fetchcb_ctx(handle);
    struct commit_observer observer = {
        .event = alpm_option_get_eventcb(handle), .event_ctx = alpm_option_get_eventcb_ctx(handle),
        .log = alpm_option_get_logcb(handle), .log_ctx = alpm_option_get_logcb_ctx(handle),
    };
    enum arch_transaction_result result = ARCH_REFUSED;
    alpm_list_t *details = NULL;
    int marker = -1;
    if (!check_baseline(handle, installed, installed_count) ||
        alpm_option_set_questioncb(handle, decline_question, &question) != 0 ||
        alpm_option_set_fetchcb(handle, deny_fetch, NULL) != 0)
        goto done;
    if (alpm_sync_sysupgrade(handle, 0) != 0 || question)
        goto done;
    for (size_t i = 0; i < target_count; ++i) {
        alpm_pkg_t *pkg = targets[i] ? alpm_find_dbs_satisfier(handle, alpm_get_syncdbs(handle), targets[i]) : NULL;
        if (!pkg || !match(alpm_pkg_get_name(pkg), targets[i]) || question)
            goto done;
        /* A requested package may already be in the full system upgrade. */
        if (!alpm_list_find_ptr(alpm_trans_get_add(handle), pkg) && alpm_add_pkg(handle, pkg) != 0)
            goto done;
    }
    for (size_t i = 0; i < expected_count; ++i)
        if (!expected[i].repo && (!add_local(handle, cache, &expected[i], local_signer) || question))
            goto done;
    if (alpm_trans_prepare(handle, &details) != 0 || question ||
        !check_additions(handle, expected, expected_count, cache) || question)
        goto done;
    if (policy_current && !policy_current(policy_context))
        goto done;
    if (alpm_option_set_eventcb(handle, observe_event, &observer) != 0 ||
        alpm_option_set_logcb(handle, observe_log, &observer) != 0)
        goto done;
    /* Keep a durable replay barrier before the first possible package write. */
    result = ARCH_OUTCOME_UNKNOWN;
    marker = openat(receipt->dirfd, receipt->id,
                    O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (marker < 0)
        goto done;
    char header[START_SIZE + 1];
    receipt_header(header, receipt);
    if (!write_all(marker, header, START_SIZE) || fsync(marker) != 0 || fsync(receipt->dirfd) != 0)
        goto done;
    /* libalpm can report success despite a post-transaction hook error. */
    if (alpm_trans_commit(handle, &details) == 0 && observer.done && !observer.error && !question)
        result = ARCH_COMMITTED;

done:
    free_details(details, alpm_errno(handle));
    if (alpm_trans_release(handle) != 0)
        result = ARCH_OUTCOME_UNKNOWN;
    if (alpm_option_set_questioncb(handle, previous, previous_ctx) != 0)
        result = ARCH_OUTCOME_UNKNOWN;
    if (alpm_option_set_fetchcb(handle, previous_fetch, previous_fetch_ctx) != 0)
        result = ARCH_OUTCOME_UNKNOWN;
    if (alpm_option_set_eventcb(handle, observer.event, observer.event_ctx) != 0)
        result = ARCH_OUTCOME_UNKNOWN;
    if (alpm_option_set_logcb(handle, observer.log, observer.log_ctx) != 0 || observer.error)
        result = ARCH_OUTCOME_UNKNOWN;
    if (marker >= 0) {
        if (result == ARCH_COMMITTED &&
            (!write_all(marker, RECEIPT_DONE, DONE_SIZE) || fsync(marker) != 0))
            result = ARCH_OUTCOME_UNKNOWN;
        if (close(marker) != 0)
            result = ARCH_OUTCOME_UNKNOWN;
    }
    return result;
}
