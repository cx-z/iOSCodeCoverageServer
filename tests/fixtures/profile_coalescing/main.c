#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

extern uint64_t __llvm_profile_get_size_for_buffer(void);
extern int __llvm_profile_write_buffer(char *);

/* Exercise distinct branch and loop regions across cumulative snapshots. */
static int exercise(int value) {
    int result = 0;
    for (int i = 0; i < value; i++) {
        if (i % 2) result += i;
        else result -= i;
    }
    return result;
}

/* Produce concurrent instrumentation updates while the main thread samples. */
static void *worker(void *unused) {
    volatile int result = 0;
    for (int i = 0; i < 50000; i++) result += exercise(i % 20);
    return NULL;
}

/* Persist a real LLVM raw buffer, without clearing the process counters. */
static void snapshot(const char *directory, int launch, int index) {
    size_t size = (size_t)__llvm_profile_get_size_for_buffer();
    char *buffer = malloc(size);
    if (!buffer || __llvm_profile_write_buffer(buffer)) exit(2);
    char filename[1024];
    snprintf(filename, sizeof(filename), "%s/%d-%d.profraw", directory, launch, index);
    FILE *file = fopen(filename, "wb");
    if (!file) exit(3);
    fwrite(buffer, size, 1, file);
    fclose(file);
    free(buffer);
}

/* Model two launches taking different paths and several background/manual samples. */
int main(int argc, char **argv) {
    int launch = atoi(argv[2]);
    volatile int result = exercise(launch ? 3 : 0);
    snapshot(argv[1], launch, 0);
    pthread_t thread;
    pthread_create(&thread, NULL, worker, NULL);
    for (int i = 1; i < 4; i++) {
        result += exercise(i * 5);
        snapshot(argv[1], launch, i);
    }
    pthread_join(thread, NULL);
    snapshot(argv[1], launch, 4);
    return 0;
}
