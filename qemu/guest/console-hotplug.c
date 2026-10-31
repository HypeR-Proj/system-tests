/* SPDX-FileCopyrightText: 2026 roolrz
 * SPDX-License-Identifier: Apache-2.0
 */

/* Linux-side acceptance; included only in the explicit hotplug test appliance. */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static void fail(const char *operation)
{
	fputs("HypeR console hotplug: FAIL: ", stderr);
	perror(operation);
	exit(1);
}

static long long now_ms(void)
{
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now))
		fail("clock_gettime");
	return now.tv_sec * 1000LL + now.tv_nsec / 1000000;
}

static int find_port(char *name, size_t capacity)
{
	DIR *directory = opendir("/sys/class/virtio-ports");
	if (!directory)
		return 0;
	struct dirent *entry;
	int found = 0;
	while ((entry = readdir(directory))) {
		char path[512], value[128] = { 0 };
		if (strncmp(entry->d_name, "vport", 5))
			continue;
		snprintf(path, sizeof(path), "/sys/class/virtio-ports/%s/name", entry->d_name);
		FILE *file = fopen(path, "r");
		if (!file)
			continue;
		int matched = fgets(value, sizeof(value), file) &&
			!strcmp(value, "org.qemu.guest_agent.0\n");
		fclose(file);
		if (matched && strlen(entry->d_name) < capacity) {
			strcpy(name, entry->d_name);
			found = 1;
			break;
		}
	}
	closedir(directory);
	return found;
}

static void wait_port(char *name, size_t capacity)
{
	long long limit = now_ms() + 180000;
	while (!find_port(name, capacity)) {
		if (now_ms() >= limit) {
			errno = ETIMEDOUT;
			fail("hotplug port discovery");
		}
		usleep(10000);
	}
}

static void echo_round(const char *name, unsigned int round)
{
	char path[512];
	snprintf(path, sizeof(path), "/dev/%s", name);
	int fd = open(path, O_RDWR | O_NONBLOCK);
	if (fd < 0)
		fail("open named port");
	unsigned char sent[4096], received[4096];
	for (unsigned int block = 0; block < 128; block++) {
		for (size_t i = 0; i < sizeof(sent); i++)
			sent[i] = (unsigned char)(i * 17 + block * 13 + round);
		size_t written = 0, read_bytes = 0;
		long long limit = now_ms() + 30000;
		while (read_bytes != sizeof(received)) {
			struct pollfd event = { fd, POLLIN | (written < sizeof(sent) ? POLLOUT : 0), 0 };
			if (poll(&event, 1, 100) < 0 && errno != EINTR)
				fail("poll named port");
			if (now_ms() >= limit || event.revents & (POLLERR | POLLHUP | POLLNVAL)) {
				errno = ETIMEDOUT;
				fail("named port stalled");
			}
			if (event.revents & POLLOUT) {
				ssize_t count = write(fd, sent + written, sizeof(sent) - written);
				if (count > 0) written += (size_t)count;
				else if (count < 0 && errno != EAGAIN && errno != EINTR) fail("write port");
			}
			if (event.revents & POLLIN) {
				ssize_t count = read(fd, received + read_bytes, sizeof(received) - read_bytes);
				if (count > 0) read_bytes += (size_t)count;
				else if (count <= 0 && errno != EAGAIN && errno != EINTR) fail("read port");
			}
		}
		if (written != sizeof(sent) || memcmp(sent, received, sizeof(sent))) {
			errno = EIO;
			fail("port data mismatch");
		}
	}
	close(fd);
	printf("HypeR console hotplug: binary round-trip %u PASS\n", round);
	fflush(stdout);
}

static void rebind(const char *name)
{
	unsigned int device, port;
	if (sscanf(name, "vport%up%u", &device, &port) != 2)
		fail("port identity");
	char value[64];
	int bytes = snprintf(value, sizeof(value), "virtio%u", device);
	const char *operations[] = { "unbind", "bind" };
	for (size_t i = 0; i < 2; i++) {
		char path[128];
		snprintf(path, sizeof(path), "/sys/bus/virtio/drivers/virtio_console/%s", operations[i]);
		int fd = open(path, O_WRONLY);
		if (fd < 0 || write(fd, value, (size_t)bytes) != bytes)
			fail("console driver rebind");
		close(fd);
	}
}

int main(void)
{
	char name[256];
	if (find_port(name, sizeof(name))) {
		fputs("HypeR console hotplug: endpoint present before takeover\n", stderr);
		return 1;
	}
	puts("HypeR console hotplug: bootstrap slot empty");
	fflush(stdout);
	/* The fixture starts storage only after this empty-slot observation. */
	int ready = open("/run/console-hotplug.ready", O_CREAT | O_EXCL | O_WRONLY, 0600);
	if (ready < 0 || close(ready))
		fail("publish bootstrap readiness");
	wait_port(name, sizeof(name));
	puts("HypeR console hotplug: named port automatically discovered");
	echo_round(name, 1);
	rebind(name);
	wait_port(name, sizeof(name));
	echo_round(name, 2);
	puts("HypeR console hotplug: acceptance PASS");
	return 0;
}
