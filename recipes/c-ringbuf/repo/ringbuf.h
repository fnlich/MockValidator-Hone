#ifndef RINGBUF_H
#define RINGBUF_H
#include <stddef.h>

/*
 * A fixed-capacity byte FIFO.
 *
 * rb_write copies as many bytes from `data` as there is free space for and
 * returns how many it copied. It never overwrites bytes that have not been
 * read yet: when the buffer is full it copies nothing and returns 0.
 *
 * rb_read removes up to `n` of the oldest unread bytes, copies them to `out`
 * in the order they were written, and returns how many it removed.
 *
 * rb_peek copies up to `n` of the oldest unread bytes to `out` without
 * removing them. It returns how many it copied, which is never more than the
 * number of unread bytes.
 */
typedef struct {
    unsigned char *buf;
    size_t cap;
    size_t head;
    size_t tail;
    size_t len;
} ringbuf;

int rb_init(ringbuf *rb, size_t capacity);
void rb_free(ringbuf *rb);
size_t rb_write(ringbuf *rb, const unsigned char *data, size_t n);
size_t rb_read(ringbuf *rb, unsigned char *out, size_t n);
size_t rb_peek(const ringbuf *rb, unsigned char *out, size_t n);
size_t rb_len(const ringbuf *rb);

#endif
