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
ringbuf rb; rb_init(&rb, 4);
const unsigned char a[] = "abcdef";
printf("w1=%zu\n", rb_write(&rb, a, 6));
printf("w2=%zu\n", rb_write(&rb, (const unsigned char*)"xy", 2));
unsigned char out[8] = {0};
size_t n = rb_read(&rb, out, 8);
printf("r=%zu [%.*s]\n", n, (int)n, out);
printf("w3=%zu\n", rb_write(&rb, (const unsigned char*)"123456", 6));
n = rb_read(&rb, out, 3); printf("r=%zu [%.*s]\n", n, (int)n, out);
n = rb_write(&rb, (const unsigned char*)"zz", 2); printf("w4=%zu len=%zu\n", n, rb_len(&rb));
n = rb_read(&rb, out, 8); printf("r=%zu [%.*s]\n", n, (int)n, out);
rb_free(&rb);
""")
