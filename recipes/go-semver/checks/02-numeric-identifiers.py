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
	pairs := [][2]string{{"1.0.0-alpha.2", "1.0.0-alpha.10"}, {"1.0.0-rc.9", "1.0.0-rc.11"}, {"1.0.0-1", "1.0.0-01a"}, {"1.10.0", "1.9.0"}, {"1.0.0-beta.11", "1.0.0-beta.2"}, {"1.0.0-x.7.z.92", "1.0.0-x.7.z.100"}, {"1.0", "1.0.0"}, {"1.0.0-", "1.0.0"}}
	for _, p := range pairs {
		c, err := semver.Compare(p[0], p[1])
		fmt.Printf("%s vs %s: %d %v\n", p[0], p[1], c, err)
	}
""")
