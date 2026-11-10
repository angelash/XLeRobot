"""Build the small local AR camera probe with an existing Android SDK/JDK.

No Gradle download, phone install, credentials in source, or public APK output.
"""
from pathlib import Path
import argparse,hashlib,json,os,subprocess,zipfile

def build(sdk,jdk,aar,output,keystore=None):
    sdk,jdk,aar,output=map(lambda p:Path(p).resolve(),(sdk,jdk,aar,output))
    output.mkdir(parents=True,exist_ok=False);source=Path(__file__).resolve().parent
    tools=sdk/'build-tools/34.0.0';android=sdk/'platforms/android-34/android.jar'
    env=os.environ.copy();env['JAVA_HOME']=str(jdk);env['PATH']=str(jdk/'bin')+os.pathsep+env['PATH']
    def run(args):subprocess.run([str(a) for a in args],env=env,check=True,timeout=60)
    library=output/'aar';zipfile.ZipFile(aar).extractall(library)
    classes=output/'classes';classes.mkdir();dex=output/'dex';dex.mkdir()
    run([jdk/'bin/javac.exe','-encoding','UTF-8','-source','8','-target','8','-classpath',str(android)+os.pathsep+str(library/'classes.jar'),'-d',classes,source/'MainActivity.java'])
    jar=output/'app-classes.jar'
    run([jdk/'bin/jar.exe','cf',jar,'-C',classes,'.'])
    run([jdk/'bin/java.exe','-cp',tools/'lib/d8.jar','com.android.tools.r8.D8','--release','--min-api','26','--lib',android,'--output',dex,jar,library/'classes.jar'])
    unsigned=output/'unsigned.apk'
    run([tools/'aapt.exe','package','-f','-M',source/'AndroidManifest.xml','-I',android,'-F',unsigned])
    with zipfile.ZipFile(unsigned,'a',zipfile.ZIP_DEFLATED) as z:
        z.write(dex/'classes.dex','classes.dex')
        for p in (library/'jni/arm64-v8a').glob('*.so'):z.write(p,'lib/arm64-v8a/'+p.name)
    key=Path(keystore).resolve() if keystore else output/'local-debug.keystore'
    if not key.exists():run([jdk/'bin/keytool.exe','-genkeypair','-keystore',key,'-storepass','android','-keypass','android','-alias','local-probe','-dname','CN=XLeRobot Local Probe','-keyalg','RSA','-keysize','2048','-validity','3650'])
    aligned=output/'aligned.apk';run([tools/'zipalign.exe','-f','4',unsigned,aligned])
    apk=output/'xlerobot-ar-probe.apk'
    run([jdk/'bin/java.exe','-jar',tools/'lib/apksigner.jar','sign','--ks',key,'--ks-pass','pass:android','--key-pass','pass:android','--out',apk,aligned])
    run([jdk/'bin/java.exe','-jar',tools/'lib/apksigner.jar','verify',apk])
    receipt={'apk':str(apk),'apk_sha256':hashlib.sha256(apk.read_bytes()).hexdigest(),'aar_sha256':hashlib.sha256(aar.read_bytes()).hexdigest(),'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.is_file()},'installed':False}
    (output/'build-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf8');print(json.dumps(receipt))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--sdk',required=True);p.add_argument('--jdk',required=True);p.add_argument('--aar',required=True);p.add_argument('--output',required=True);p.add_argument('--keystore')
    args=p.parse_args();build(args.sdk,args.jdk,args.aar,args.output,args.keystore)
