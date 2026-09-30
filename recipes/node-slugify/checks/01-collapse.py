import subprocess, sys
r = subprocess.run(['node', '-e', "const path = require('path');\nconst { slugify } = require(path.resolve('index.js'));\nfunction show(t, o) { console.log(JSON.stringify(t), JSON.stringify(o || {}), '->', JSON.stringify(slugify(t, o))); }\nshow('Hello, World!');\nshow('  Café   au  lait  ');\nshow('a -- b __ c');\nshow('Ünïcödé Ståff');\nshow('---');\nshow('C++ & Rust: 2 langs');\n"], capture_output=True, timeout=60)
sys.stdout.write(r.stdout.decode()); print('exit', r.returncode)
