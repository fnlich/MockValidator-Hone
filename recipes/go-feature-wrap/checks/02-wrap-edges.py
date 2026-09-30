import os, shutil, subprocess, sys, tempfile
def run(body):
    tmp = tempfile.mkdtemp(prefix="chk")
    work = os.path.join(tmp, "w")
    shutil.copytree(".", work)
    os.makedirs(os.path.join(work, "cmd", "chk"), exist_ok=True)
    open(os.path.join(work, "cmd", "chk", "main.go"), "w").write('package main\n\nimport (\n\t"fmt"\n\t"example.com/textutil"\n)\n\nfunc main() {\n' + body + '\n}\n')
    env = dict(os.environ, HOME=tmp, GOCACHE=os.path.join(tmp, "gocache"), GOPATH=os.path.join(tmp, "gopath"), GOFLAGS="-mod=mod", GOPROXY="off", GOTOOLCHAIN="local", CGO_ENABLED="0")
    r = subprocess.run(["go", "run", "./cmd/chk"], cwd=work, env=env, capture_output=True, timeout=240)
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    if r.returncode:
        print("run failed")
    print("exit", r.returncode)
run(r"""
	cases := []struct{ s string; w int }{{"supercalifragilistic is long", 6}, {"", 5}, {"   ", 5}, {"one two", 0}, {"héllo wörld ñ", 7}, {"a", -1}}
	for _, c := range cases {
		lines := textutil.Wrap(c.s, c.w)
		fmt.Printf("%q w=%d -> %d nil=%v %q\n", c.s, c.w, len(lines), lines == nil, lines)
	}
	fmt.Println(textutil.Truncate("hello world", 8), textutil.Words(" a  b "))
""")
