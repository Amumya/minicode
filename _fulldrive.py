import subprocess,sys,time,io
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")
t=time.time()
r=subprocess.run([sys.executable,"-m","pytest","-q","--no-header"],capture_output=True,text=True,timeout=600)
print("elapsed %.1fs rc=%s"%(time.time()-t,r.returncode))
print(r.stdout.strip().splitlines()[-1])
open("_full_run.log","w",encoding="utf-8").write(r.stdout[-3000:])
