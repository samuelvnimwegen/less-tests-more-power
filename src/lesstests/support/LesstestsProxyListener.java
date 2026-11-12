package lesstests;

import io.restassured.RestAssured;
import io.restassured.config.SSLConfig;
import java.io.File;
import org.testng.ISuite;
import org.testng.ISuiteListener;

/**
 * Sends every RestAssured request of the run through the lesstests proxy, without touching the test code.
 *
 * RestAssured builds its own Apache client, which ignores the https.proxyHost properties of the JVM, so the static
 * RestAssured settings are the only way in. lesstests puts this file into its own copy of the project and names it
 * with TestNG's -listener flag. Without the lesstests.proxy.host property it does nothing.
 */
public class LesstestsProxyListener implements ISuiteListener {

    @Override
    public void onStart(ISuite suite) {
        String host = System.getProperty("lesstests.proxy.host");
        if (host == null || host.isEmpty()) {
            return;
        }
        int port = Integer.parseInt(System.getProperty("lesstests.proxy.port"));
        RestAssured.proxy(host, port);
        String truststore = System.getProperty("lesstests.truststore");
        if (truststore != null && !truststore.isEmpty()) {
            String password = System.getProperty("lesstests.truststore.password", "");
            SSLConfig ssl = SSLConfig.sslConfig().trustStore(new File(truststore), password).trustStoreType("PKCS12");
            RestAssured.config = RestAssured.config().sslConfig(ssl);
        }
    }

    @Override
    public void onFinish(ISuite suite) {
    }
}
