import java.util.Properties
plugins { id("com.android.application") }
// Release builds are signed with a key kept outside the repo: ~/.config/canvas-widget/signing.properties
// (storeFile, storePassword, keyAlias, keyPassword). Without it, release builds fall back to the debug key.
val sign = Properties().apply { val f = file(System.getProperty("user.home") + "/.config/canvas-widget/signing.properties"); if (f.exists()) f.inputStream().use { load(it) } }
android {
    namespace = "ca.voiceclaw.canvaswidget"
    compileSdk = 36
    defaultConfig {
        applicationId = "ca.voiceclaw.canvaswidget"
        minSdk = 31; targetSdk = 36; versionCode = 2; versionName = "0.2"
    }
    signingConfigs {
        if (sign.getProperty("storeFile") != null) create("release") {
            storeFile = file(sign.getProperty("storeFile")); storePassword = sign.getProperty("storePassword")
            keyAlias = sign.getProperty("keyAlias"); keyPassword = sign.getProperty("keyPassword")
        }
    }
    buildTypes {
        release { isMinifyEnabled = false; signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug") }
    }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
}
