import os
import sys
import time
import subprocess

def main():
    print("⛩ Sky Shrine - Server Watchdog Started")
    print("=======================================")
    
    script_path = os.path.join(os.path.dirname(__file__), "app.py")
    
    while True:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting app.py...")
        # Start app.py as a subprocess
        process = subprocess.Popen([sys.executable, script_path])
        
        # Wait for the subprocess to finish
        process.wait()
        
        # Check the exit code
        if process.returncode == 42:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Restart requested (Exit Code 42). Rebooting in 2 seconds...")
            time.sleep(2)
        else:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] app.py exited with code {process.returncode}. Stopping Watchdog.")
            break

if __name__ == "__main__":
    main()
