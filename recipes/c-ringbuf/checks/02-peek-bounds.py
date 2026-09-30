import os, subprocess, sys, tempfile
def run(driver):
    tmp = tempfile.mkdtemp(prefix="chk")
    src = os.path.join(tmp, "d.c"); exe = os.path.join(tmp, "d")
    open(src, "w").write('#include "ringbuf.h"\n#include <stdio.h>\n#include <string.h>\nint main(void){\n' + driver + '\nreturn 0;}\n')
    r = subprocess.run(["gcc", "-std=c11", "-O1", "-I.", "ringbuf.c", src, "-o", exe], capture_output=True, timeout=60)
    if r.returncode:
        print("build failed"); return
    r = subprocess.run([exe], capture_output=True, timeout=20)
    sys.stdout.write(r.stdout.decode("latin-1")); print("exit", r.returncode)
run(r"""
ringbuf rb; rb_init(&rb, 8);
unsigned char out[16]; memset(out, '#', sizeof out);
rb_write(&rb, (const unsigned char*)"hello", 5);
size_t n = rb_peek(&rb, out, 16);
printf("peek=%zu [%.*s] len=%zu\n", n, (int)n, out, rb_len(&rb));
rb_read(&rb, out, 3);
rb_write(&rb, (const unsigned char*)"worldwide", 9);
memset(out, '#', sizeof out);
n = rb_peek(&rb, out, 16);
printf("peek=%zu [%.*s] len=%zu\n", n, (int)n, out, rb_len(&rb));
n = rb_peek(&rb, out, 2);
printf("peek2=%zu [%.*s]\n", n, (int)n, out);
rb_free(&rb);
""")
