import os, shutil, subprocess, sys, tempfile
def run(body):
    tmp = tempfile.mkdtemp(prefix="chk")
    work = os.path.join(tmp, "w")
    shutil.copytree(".", work)
    os.makedirs(os.path.join(work, "cmd", "chk"), exist_ok=True)
    open(os.path.join(work, "cmd", "chk", "main.go"), "w").write('package main\n\nimport (\n\t"fmt"\n\t"example.com/semver"\n)\n\nfunc main() {\n' + body + '\n}\n')
    env = dict(os.environ, HOME=tmp, GOCACHE=os.path.join(tmp, "gocache"), GOPATH=os.path.join(tmp, "gopath"), GOFLAGS="-mod=mod", GOPROXY="off", GOTOOLCHAIN="local", CGO_ENABLED="0")
    r = subprocess.run(["go", "run", "./cmd/chk"], cwd=work, env=env, capture_output=True, timeout=240)
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    if r.returncode:
        print("run failed")
    print("exit", r.returncode)
run(r"""
	pairs := [][2]string{{"1.0.0-alpha", "1.0.0"}, {"1.0.0", "1.0.0-rc.1"}, {"2.0.0-rc.1", "1.9.9"}, {"1.0.0+build.5", "1.0.0"}, {"1.2.3-beta", "1.2.3-beta+exp.sha"}, {"1.0.0-alpha", "1.0.0-alpha.1"}, {"1.0.0-alpha.beta", "1.0.0-alpha.1"}}
	for _, p := range pairs {
		c, err := semver.Compare(p[0], p[1])
		fmt.Printf("%s vs %s: %d %v\n", p[0], p[1], c, err)
	}
""")
