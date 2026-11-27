#define _GNU_SOURCE

#include "arch_transaction.h"

#include <fcntl.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int receipt_directory(void)
{
    const char *const path[] = {"/", "var", "lib", "pennix-lifecycle", "arch-transactions"};
    int parent = AT_FDCWD;
    for (size_t i = 0; i < sizeof(path) / sizeof(*path); ++i) {
        int fd = openat(parent, path[i], O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (parent != AT_FDCWD)
            close(parent);
        struct stat st;
        if (fd < 0)
            return -1;
        if (fstat(fd, &st) != 0 || st.st_uid != 0 || (st.st_mode & 022)) {
            close(fd);
            return -1;
        }
        parent = fd;
    }
    return parent;
}

int main(int argc, char *argv[])
{
    if (geteuid() != 0 || argc != 4 || strcmp(argv[1], "observe") != 0)
        return ARCH_OUTCOME_UNKNOWN;
    const struct arch_transaction_receipt receipt = {
        .dirfd = receipt_directory(),
        .id = argv[2],
        .request_sha256 = argv[3],
    };
    if (receipt.dirfd < 0)
        return ARCH_OUTCOME_UNKNOWN;
    enum arch_transaction_result result = arch_observe_receipt(&receipt);
    close(receipt.dirfd);
    return result;
}
