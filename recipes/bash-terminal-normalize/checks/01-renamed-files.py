import hashlib, os
for name in sorted(os.listdir("inbox")):
    data = open(os.path.join("inbox", name), "rb").read()
    print(repr(name), hashlib.sha256(data).hexdigest()[:12])
