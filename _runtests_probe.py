import subprocess,sys,time,io
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")
env={"PYTHONPATH":"src","PATH":""}
import os
e=dict(os.environ); e["PYTHONPATH"]="src"; e.pop("MINICODE_REDIS_URL",None)
t=time.time()
try:
    r=subprocess.run([sys.executable,"tests/run_tests.py"],capture_output=True,text=True,timeout=120,env=e)
    out=r.stdout
    fails=[l for l in out.splitlines() if "❌" in l or "失败" in l]
    print("rc=%s elapsed=%.1fs"%(r.returncode,time.time()-t))
    print("SUMMARY:", [l for l in out.splitlines() if l.startswith("通过")])
    print("num failure lines:", len(fails))
    for l in fails[:15]: print("  ",l.strip())
except subprocess.TimeoutExpired:
    print("run_tests.py TIMEOUT >120s")
