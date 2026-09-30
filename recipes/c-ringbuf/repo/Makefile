CFLAGS ?= -std=c11 -O1 -Wall

libringbuf.a: ringbuf.o
	ar rcs $@ $^

ringbuf.o: ringbuf.c ringbuf.h
	$(CC) $(CFLAGS) -c ringbuf.c -o $@

clean:
	rm -f ringbuf.o libringbuf.a
