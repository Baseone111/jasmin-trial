import configparser
import hashlib

from scripts.configure_gateway import configure


def test_gateway_credentials_and_logging_format_are_preserved(tmp_path):
    path=tmp_path/"jasmin.cfg"
    path.write_text("[http-api]\nlog_format = %(asctime)s %(message)s\nport = 9999\n[jcli]\nadmin_username = old\n[amqp-broker]\nhost = first\nhost = last\n")
    path.chmod(0o640)
    env={"JCLI_PASSWORD":"a-new-password","AMQP_BROKER_PASSWORD":"broker-secret"}
    configure(path,env)
    config=configparser.ConfigParser(interpolation=None)
    config.read(path)
    assert config["jcli"]["admin_username"]=="labadmin"
    assert config["jcli"]["admin_password"]==hashlib.md5(b"a-new-password").hexdigest()
    assert config["amqp-broker"]["username"]=="jasmin"
    assert config["amqp-broker"]["password"]=="broker-secret"
    assert config["http-api"]["log_format"]=="%(asctime)s %(message)s"
    assert config["http-api"]["port"]=="1401"
    assert path.stat().st_mode & 0o777 == 0o640
    configure(path,env)
    config.read(path)
    assert config["jcli"]["authentication"]=="True"
