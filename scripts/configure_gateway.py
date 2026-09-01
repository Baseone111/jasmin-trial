"""Set explicit lab configuration, then preserve the official image entrypoint.

Setting the INI fields as well as environment variables avoids depending on
which environment overrides a particular upstream image supports.
"""
import configparser
import hashlib
import os
from pathlib import Path
import sys


def configure(path,env):
    path=Path(path)
    if not path.is_file():
        raise RuntimeError("The official image did not provide /etc/jasmin/jasmin.cfg")
    config=configparser.ConfigParser(interpolation=None,strict=False)
    config.read(path)
    fields={
        "amqp-broker":{
            "host":env.get("AMQP_BROKER_HOST","rabbitmq"),
            "port":"5672",
            "username":env.get("AMQP_BROKER_USERNAME","jasmin"),
            "password":env.get("AMQP_BROKER_PASSWORD","local-broker-only"),
        },
        "redis-client":{"host":env.get("REDIS_CLIENT_HOST","redis"),"port":"6379"},
        "jcli":{
            "bind":"0.0.0.0","port":"8990","authentication":"True",
            "admin_username":"labadmin",
            # Jasmin's documented jCli format uses an MD5 digest, not plaintext.
            "admin_password":hashlib.md5(env.get("JCLI_PASSWORD","local-admin-only").encode()).hexdigest(),
        },
        "http-api":{"bind":"0.0.0.0","port":"1401"},
        "dlr-thrower":{"retry_delay":"2","max_retries":"10"},
    }
    for section,values in fields.items():
        if not config.has_section(section):
            config.add_section(section)
        for key,value in values.items():
            config.set(section,key,value)
    # Write beside the original and atomically replace so interrupted startup
    # does not leave a partially written configuration.
    temporary=path.with_suffix(".cfg.tmp")
    with temporary.open("w") as handle:
        config.write(handle)
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)


if __name__=="__main__":
    configure("/etc/jasmin/jasmin.cfg",os.environ)
    os.execv("/docker-entrypoint.sh",["/docker-entrypoint.sh",*sys.argv[1:]])
