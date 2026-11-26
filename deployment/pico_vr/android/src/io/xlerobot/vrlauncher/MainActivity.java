package io.xlerobot.vrlauncher;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.security.KeyChain;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * PICO launcher for the XLeRobot Studio VR page.
 *
 * It only stores the robot address, checks that Studio answers, installs the
 * robot's local CA certificate (so the browser accepts https on port 8770) and
 * opens the WebXR page in the headset browser. All robot control happens in
 * that page through the existing leased Studio session.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "robot";
    private static final String DEFAULT_HOST = "192.168.137.150";
    private static final int PORT = 8770;
    private static final Pattern HOST = Pattern.compile("^[A-Za-z0-9.\\-]{1,253}$");

    private EditText address;
    private TextView status;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        int pad = dp(20);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);

        TextView title = text("XLeRobot PICO 遥控", 26);
        title.setTextColor(Color.WHITE);
        root.addView(title);
        root.addView(text("连接机器人热点后，填写车载笔记本地址，然后打开 VR 遥控页。手柄映射和安全说明在页面里。", 16));

        root.addView(text("机器人地址", 15));
        address = new EditText(this);
        address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setText(prefs.getString("host", DEFAULT_HOST));
        root.addView(address);

        root.addView(button("① 检测连接", new View.OnClickListener() {
            public void onClick(View v) { check(); }
        }));
        root.addView(button("② 安装机器人证书（首次使用）", new View.OnClickListener() {
            public void onClick(View v) { installCertificate(); }
        }));
        root.addView(button("③ 打开 VR 遥控", new View.OnClickListener() {
            public void onClick(View v) { open(false); }
        }));
        root.addView(button("用其他浏览器打开（如 Wolvic）", new View.OnClickListener() {
            public void onClick(View v) { open(true); }
        }));

        status = text("", 15);
        status.setTextColor(Color.rgb(253, 224, 71));
        root.addView(status);
        root.addView(text("证书只让浏览器信任这台机器人的 https 页面；安装时系统会显示证书指纹，可与车载安装程序输出的指纹核对。", 13));

        ScrollView scroll = new ScrollView(this);
        scroll.addView(root);
        setContentView(scroll);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private TextView text(String value, int size) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setPadding(0, dp(6), 0, dp(6));
        return view;
    }

    private Button button(String label, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setGravity(Gravity.CENTER);
        button.setOnClickListener(listener);
        return button;
    }

    private String host() {
        String value = address.getText().toString().trim();
        if (value.startsWith("http://")) value = value.substring(7);
        if (value.startsWith("https://")) value = value.substring(8);
        int cut = value.indexOf(':');
        if (cut >= 0) value = value.substring(0, cut);
        cut = value.indexOf('/');
        if (cut >= 0) value = value.substring(0, cut);
        if (!HOST.matcher(value).matches()) {
            show("地址格式不对，例如 " + DEFAULT_HOST);
            return null;
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString("host", value).apply();
        return value;
    }

    private void show(final String message) {
        runOnUiThread(new Runnable() {
            public void run() { status.setText(message); }
        });
    }

    private static byte[] get(String url) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(3000);
        connection.setReadTimeout(4000);
        connection.setUseCaches(false);
        try {
            int code = connection.getResponseCode();
            InputStream stream = code < 400 ? connection.getInputStream() : connection.getErrorStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int n;
            while (stream != null && (n = stream.read(buffer)) > 0 && out.size() < 1 << 20) out.write(buffer, 0, n);
            if (code >= 400) throw new Exception("HTTP " + code);
            return out.toByteArray();
        } finally {
            connection.disconnect();
        }
    }

    private static String field(String json, String name) {
        Matcher m = Pattern.compile("\"" + name + "\"\\s*:\\s*(\"([^\"]*)\"|true|false|-?[0-9.]+)").matcher(json);
        if (!m.find()) return null;
        return m.group(2) != null ? m.group(2) : m.group(1);
    }

    private void check() {
        final String host = host();
        if (host == null) return;
        show("正在连接 " + host + " …");
        new Thread(new Runnable() {
            public void run() {
                StringBuilder result = new StringBuilder();
                try {
                    String studio = new String(get("http://" + host + ":" + PORT + "/api/platform/status"), "UTF-8");
                    result.append("Studio 在线，版本 ").append(field(studio, "version")).append("\n");
                } catch (Exception e) {
                    show("连不上 Studio（" + e.getMessage() + "）。请确认 PICO 已连机器人热点、笔记本已开机，并且防火墙已放行本机地址。");
                    return;
                }
                try {
                    String web = new String(get("http://" + host + ":" + PORT + "/api/web/status"), "UTF-8");
                    String mode = field(web, "mode");
                    result.append("控制服务在线；网页控制")
                          .append("true".equals(field(web, "active")) ? "进行中" : "未开启")
                          .append(mode == null ? "（车载版本还没有 VR 接口，请先安装车载更新包）" : "").append("\n");
                } catch (Exception e) {
                    result.append("控制服务未响应（").append(e.getMessage()).append("）\n");
                }
                try {
                    get("http://" + host + ":" + PORT + "/vr/ca.crt");
                    result.append("HTTPS 已配置，可以安装证书并打开 VR 页");
                } catch (Exception e) {
                    result.append("HTTPS 未配置（").append(e.getMessage()).append("）：VR 页需要车载更新包配置 HTTPS");
                }
                show(result.toString());
            }
        }).start();
    }

    private void installCertificate() {
        final String host = host();
        if (host == null) return;
        show("正在下载证书 …");
        new Thread(new Runnable() {
            public void run() {
                try {
                    byte[] pem = get("http://" + host + ":" + PORT + "/vr/ca.crt");
                    CertificateFactory factory = CertificateFactory.getInstance("X.509");
                    final X509Certificate cert = (X509Certificate) factory.generateCertificate(new ByteArrayInputStream(pem));
                    if (cert.getBasicConstraints() < 0) throw new Exception("这不是根证书");
                    final byte[] der = cert.getEncoded();
                    final String fingerprint = hex(MessageDigest.getInstance("SHA-256").digest(der));
                    runOnUiThread(new Runnable() {
                        public void run() {
                            status.setText("证书 SHA-256：\n" + fingerprint + "\n请与车载安装程序输出的指纹核对，一致再确认安装（用途选“VPN和应用”或“CA证书”）。");
                            Intent intent = KeyChain.createInstallIntent();
                            intent.putExtra(KeyChain.EXTRA_CERTIFICATE, der);
                            intent.putExtra(KeyChain.EXTRA_NAME, "XLeRobot Studio");
                            try {
                                startActivity(intent);
                            } catch (ActivityNotFoundException e) {
                                status.append("\n系统没有证书安装界面：请在浏览器打开 http://" + host + ":" + PORT + "/vr/ca.crt 下载后到设置中安装。");
                            }
                        }
                    });
                } catch (Exception e) {
                    show("证书下载失败（" + e.getMessage() + "）。车载需要先安装更新包并配置 HTTPS。");
                }
            }
        }).start();
    }

    private static String hex(byte[] data) {
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < data.length; i++) {
            if (i > 0) out.append(i % 8 == 0 ? "\n" : ":");
            out.append(String.format("%02X", data[i] & 0xff));
        }
        return out.toString();
    }

    private void open(boolean choose) {
        String host = host();
        if (host == null) return;
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse("https://" + host + ":" + PORT + "/vr/"));
        intent.addCategory(Intent.CATEGORY_BROWSABLE);
        try {
            startActivity(choose ? Intent.createChooser(intent, "选择支持 WebXR 的浏览器") : intent);
            show("已在浏览器打开 VR 遥控页。若提示证书不受信任，请先完成第②步，或在提示页选择继续访问。");
        } catch (ActivityNotFoundException e) {
            show("没有可用的浏览器。请在 PICO 商店安装 PICO 浏览器或 Wolvic。");
        }
    }
}
