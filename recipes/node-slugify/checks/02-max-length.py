import subprocess, sys
r = subprocess.run(['node', '-e', "const path = require('path');\nconst { slugify } = require(path.resolve('index.js'));\nfunction show(t, o) { console.log(JSON.stringify(t), JSON.stringify(o || {}), '->', JSON.stringify(slugify(t, o))); }\nshow('The quick brown fox jumps', {maxLength: 15});\nshow('hello world', {maxLength: 6});\nshow('abcdefghij', {maxLength: 4});\nshow('ab cd ef', {maxLength: 5});\nshow('short', {maxLength: 50});\nshow('one two', {maxLength: 3});\n"], capture_output=True, timeout=60)
sys.stdout.write(r.stdout.decode()); print('exit', r.returncode)
