import os
p = "inbox/INDEX"
print(open(p, "rb").read().decode() if os.path.exists(p) else "INDEX missing", end="")
