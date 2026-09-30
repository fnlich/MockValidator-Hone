import os
def show(path):
    if not os.path.isfile(path):
        print(path, "missing"); return
    data = open(path, "rb").read()
    print(path, len(data), "bytes"); print(data.decode("utf-8", "replace"), end="")
    print("<EOF newline=%s>" % data.endswith(b"\n"))
show("report/summary.txt")
