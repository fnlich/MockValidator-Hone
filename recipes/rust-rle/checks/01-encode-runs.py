import os, subprocess, sys, tempfile
def run(body):
    tmp = tempfile.mkdtemp(prefix="chk")
    lib = os.path.join(tmp, "librle.rlib"); drv = os.path.join(tmp, "d.rs"); exe = os.path.join(tmp, "d")
    open(drv, "w").write("fn main() {\n" + body + "\n}\n")
    r = subprocess.run(["rustc", "--edition", "2021", "--crate-type", "rlib", "--crate-name", "rle", "src/lib.rs", "-o", lib], capture_output=True, timeout=120)
    if r.returncode:
        print("build failed"); return
    r = subprocess.run(["rustc", "--edition", "2021", drv, "--extern", "rle=" + lib, "-o", exe], capture_output=True, timeout=120)
    if r.returncode:
        print("link failed"); return
    r = subprocess.run([exe], capture_output=True, timeout=20)
    sys.stdout.write(r.stdout.decode("utf-8", "replace")); print("exit", r.returncode)
run(r"""
for s in ["aaab", "", "x", "aaaaaaaaaa", "aaaaaaaaaaaab", "ééé", "ab", "zzzzzzzzzzzzzzzzzzzzzzz"] {
    println!("{:?} -> {:?}", s, rle::encode(s));
}
let long: String = std::iter::repeat('q').take(101).collect();
println!("101q -> {}", rle::encode(&long));
""")
