#include "ringbuf.h"
#include <stdlib.h>

int rb_init(ringbuf *rb, size_t capacity) {
    rb->buf = malloc(capacity ? capacity : 1);
    if (!rb->buf) return -1;
    rb->cap = capacity;
    rb->head = rb->tail = rb->len = 0;
    return 0;
}

void rb_free(ringbuf *rb) {
    free(rb->buf);
    rb->buf = NULL;
}

size_t rb_write(ringbuf *rb, const unsigned char *data, size_t n) {
    size_t written = 0;
    while (written < n && rb->len < rb->cap) {
        rb->buf[rb->tail] = data[written++];
        rb->tail = (rb->tail + 1) % rb->cap;
        rb->len++;
    }
    return written;
}

size_t rb_read(ringbuf *rb, unsigned char *out, size_t n) {
    size_t got = 0;
    while (got < n && rb->len > 0) {
        out[got++] = rb->buf[rb->head];
        rb->head = (rb->head + 1) % rb->cap;
        rb->len--;
    }
    return got;
}

size_t rb_peek(const ringbuf *rb, unsigned char *out, size_t n) {
    size_t got = 0, pos = rb->head;
    while (got < n && got < rb->len) {
        out[got++] = rb->buf[pos];
        pos = (pos + 1) % rb->cap;
    }
    return got;
}

size_t rb_len(const ringbuf *rb) {
    return rb->len;
}
