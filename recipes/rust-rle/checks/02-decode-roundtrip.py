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
for s in ["12x", "3a1b", "10a2b", "1é", "", "0a", "a", "12", "2é1x100y"] {
    match rle::decode(s) {
        Ok(v) => println!("{:?} -> ok {:?} ({} chars)", s, if v.len() > 20 { format!("{}...", &v[..20]) } else { v.clone() }, v.chars().count()),
        Err(e) => println!("{:?} -> err {}", s, e),
    }
}
for s in ["aaaaaaaaaaaabbc", "ééééééééééé"] {
    let e = rle::encode(s);
    println!("roundtrip {:?} -> {:?} -> {:?}", s, e, rle::decode(&e));
}
""")
