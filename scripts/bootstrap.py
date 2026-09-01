"""Configure the dedicated lab gateway via jCli. Python 3.12 uses telnetlib.

No real provider host is accepted. Re-running on this dedicated lab is safe.
Never point this script at an existing production gateway.
"""
import os
import re
import telnetlib
import time


def safe_env(name,default):
    value = os.getenv(name,default)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,30}",value):
        raise ValueError(f"{name} must contain 1-30 letters, digits, dots, underscores or hyphens")
    return value


class Console:
    def __init__(self):
        self.conn = telnetlib.Telnet(os.getenv("JCLI_HOST","jasmin"),8990,timeout=10)
        self.wait(b"Username:")
        self.conn.write((safe_env("JCLI_USERNAME","labadmin")+"\n").encode())
        self.wait(b"Password:")
        self.conn.write((safe_env("JCLI_PASSWORD","local-admin-only")+"\n").encode())
        self.wait(b"jcli : ")

    def wait(self,marker):
        data = self.conn.read_until(marker,timeout=15)
        if marker not in data:
            raise RuntimeError("jCli did not reach its expected prompt. Check authentication and gateway logs.")
        return data.decode("utf-8",errors="replace")

    def command(self,command,*,editing=False):
        self.conn.write((command+"\n").encode())
        text = self.wait(b"> " if editing else b"jcli : ")
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]","",text)
        if re.search(r"(?i)(unknown command|error:|invalid |failed|incorrect|unknown (group|connector)|not found)",clean):
            # Do not echo commands which could contain passwords.
            raise RuntimeError("A jCli command failed. Check the gateway configuration.")
        return clean

    def configure(self,begin,fields):
        self.command(begin,editing=True)
        for key,value in fields:
            self.command(f"{key} {value}",editing=True)
        return self.command("ok")


def main():
    console = Console()
    username = safe_env("JASMIN_USERNAME","demo-app")
    password = safe_env("JASMIN_PASSWORD","local-sms-only")
    try:
        if not re.search(r"(?m)^#?demo\s",console.command("group -l")):
            console.configure("group -a",[("gid","demo")])
        users = console.command("user -l")
        if re.search(r"(?m)^#?demo-user\s",users):
            console.configure("user -u demo-user",[("password",password)])
            if username not in users:
                raise RuntimeError("Existing lab username differs. Use the original username or reset the lab.")
        else:
            console.configure("user -a",[("uid","demo-user"),("gid","demo"),("username",username),("password",password)])
        connectors = console.command("smppccm -l")
        if not re.search(r"(?m)^#?simulator\s",connectors):
            console.configure("smppccm -a",[("cid","simulator"),("host","simulator"),("port","2775"),
                                          ("username","sim-user"),("password","sim-pass"),
                                          ("bind","transceiver"),("submit_throughput","5"),
                                          ("requeue_delay","2"),("con_fail_delay","2"),("con_loss_delay","2")])
        route = console.command("mtrouter -l")
        if "DefaultRoute" not in route:
            console.configure("mtrouter -a",[("type","DefaultRoute"),("connector","smppc(simulator)"),("rate","0.0")])
        elif "simulator" not in route:
            raise RuntimeError("A different default route already exists; refusing to replace it")
        connectors = console.command("smppccm -l")
        if not re.search(r"(?m)^#?simulator\s+started\s",connectors):
            console.command("smppccm -1 simulator")
        console.command("persist")
        for _ in range(30):
            if re.search(r"(?m)^#?simulator\s+started\s+BOUND_TRX",console.command("smppccm -l")):
                print("Lab configured. Simulator is BOUND_TRX. HTTP user and route are ready.")
                return
            time.sleep(1)
        raise RuntimeError("Simulator did not bind within 30 seconds. Inspect docker compose logs jasmin simulator.")
    finally:
        console.conn.close()


if __name__=="__main__":
    main()
