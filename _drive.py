import subprocess, sys, pathlib, io
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")
py=sys.executable
files=sorted(pathlib.Path("tests").glob("test_*.py"))
out=open("_per_file.log","w",encoding="utf-8")
for f in files:
    try:
        r=subprocess.run([py,"-m","pytest",str(f),"-q","--no-header","-p","no:cacheprovider"],
                         capture_output=True,text=True,timeout=35)
        tail=[l for l in r.stdout.splitlines() if "passed" in l or "failed" in l or "error" in l.lower()]
        line=(tail[-1].strip() if tail else r.stdout.strip()[-160:])
        out.write(f"OK-TERM {f.name} rc={r.returncode} :: {line}\n")
    except subprocess.TimeoutExpired:
        out.write(f"TIMEOUT  {f.name} >35s\n")
    out.flush()
out.write("DONE\n"); out.close()
