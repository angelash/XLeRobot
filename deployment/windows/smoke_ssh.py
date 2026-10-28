"""Local SSH client verification against a simulated server. No Windows SSH service installed.

Developer-only optional dependency: paramiko. Never executes received remote commands.
"""
from pathlib import Path
import socket
import subprocess
import threading
import uuid
import logging
import os
import faulthandler
import paramiko


def run():
    faulthandler.dump_traceback_later(15)
    root=Path(__file__).resolve().parents[2]
    folder=root/'.local-deploy'/('ssh-smoke-'+uuid.uuid4().hex[:8]);folder.mkdir(parents=True)
    for name in ('host_ed25519','controller_ed25519'):
        subprocess.run(['ssh-keygen.exe','-q','-t','ed25519','-f',str(folder/name),'-N','','-C','SIMULATED_TEST_ONLY'],check=True)
    host=paramiko.Ed25519Key.from_private_key_file(str(folder/'host_ed25519'))
    controller=paramiko.Ed25519Key.from_private_key_file(str(folder/'controller_ed25519'))
    logger=logging.getLogger('paramiko');logger.addHandler(logging.NullHandler());logger.propagate=False
    commands=[];transports=[];done=threading.Event()
    class Server(paramiko.ServerInterface):
        def check_auth_publickey(self,username,key):return paramiko.AUTH_SUCCESSFUL if username=='robot' and key==controller else paramiko.AUTH_FAILED
        def get_allowed_auths(self,username):return 'publickey'
        def check_channel_request(self,kind,id):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        def check_channel_exec_request(self,channel,command):
            commands.append(command.decode())
            def reply():
                threading.Event().wait(.05)
                channel.sendall(b'SIMULATED_SSH_COMMAND_OK\n');channel.send_exit_status(0);channel.close()
            threading.Thread(target=reply,daemon=True).start();return True
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.3)
    port=listener.getsockname()[1]
    def connection(sock):
        transport=paramiko.Transport(sock);transports.append(transport)
        try:
            transport.add_server_key(host);transport.start_server(server=Server())
            while transport.is_active() and not done.is_set():
                channel=transport.accept(.2)
                if channel:
                    while not channel.closed and transport.is_active() and not done.wait(.1):
                        if channel.exit_status_ready():channel.close();break
        except (EOFError,paramiko.SSHException,OSError):pass
        finally:transport.close()
    def accept():
        while not done.is_set():
            try:sock,_=listener.accept()
            except socket.timeout:continue
            except OSError:break
            threading.Thread(target=connection,args=(sock,),daemon=True).start()
    threading.Thread(target=accept,daemon=True).start()
    fingerprint=subprocess.check_output(['ssh-keygen.exe','-lf',str(folder/'host_ed25519.pub')],text=True).split()[1]
    common=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File']
    env={k:v for k,v in os.environ.items() if k.upper()!='PSMODULEPATH'}
    try:
        trust=[*common,str(root/'deployment/windows/trust-robot.ps1'),'-RobotHost','127.0.0.1','-Port',str(port),'-ProfileDirectory',str(folder),'-Fingerprint',fingerprint]
        first=subprocess.run(trust,capture_output=True,timeout=20,env=env)
        print('Host verification completed',first.returncode,flush=True)
        assert first.returncode==0,first.stderr.decode(errors='replace')
        saved=(folder/'known_hosts').read_bytes()
        wrong=[*trust[:-1],'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA']
        assert subprocess.run(wrong,capture_output=True,timeout=20,env=env).returncode!=0
        assert (folder/'known_hosts').read_bytes()==saved
        connect=[*common,str(root/'deployment/windows/connect-robot.ps1'),'-RobotHost','127.0.0.1','-Port',str(port),'-UserName','robot','-ProfileDirectory',str(folder),'-Action','Info']
        result=subprocess.run(connect,capture_output=True,timeout=20,env=env)
        print('SSH command completed',result.returncode,flush=True)
        assert result.returncode==0,result.stderr.decode(errors='replace')
        assert b'SIMULATED_SSH_COMMAND_OK' in result.stdout
        assert commands and '-EncodedCommand' in commands[-1]
        print('SSH SIMULATION PASS: native Windows client, public-key authentication, exact host fingerprint, rejected mismatch, encoded remote command. New laptop is not yet connected.')
    finally:
        faulthandler.cancel_dump_traceback_later()
        done.set();listener.close()
        for transport in transports:transport.close()


if __name__=='__main__':run()
