import java.util.Properties

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// 正式签名(W30,决策 43):android/key.properties(不进仓库)写着 keystore 在哪、口令。
// 有它就用正式钥匙签 release;没有就退回调试钥匙(CI、开发机能编)—— 发给客户的包要加
// -Pd1max.requireReleaseKey=true,没有正式钥匙就直接失败,不会悄悄发出调试签名的包。
val keyProps = Properties().apply {
    val f = rootProject.file("key.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val haveReleaseKey = keyProps.getProperty("storeFile") != null
if (!haveReleaseKey && (project.findProperty("d1max.requireReleaseKey") as String?) == "true") {
    throw GradleException("没有 android/key.properties:发给客户的包必须用正式钥匙签(见 docs/W30-完工报告.md)")
}

android {
    namespace = "com.d1max.d1max_patrol"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.d1max.d1max_patrol"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (haveReleaseKey) {
            create("release") {
                storeFile = file(keyProps.getProperty("storeFile"))
                storePassword = keyProps.getProperty("storePassword")
                keyAlias = keyProps.getProperty("keyAlias")
                keyPassword = keyProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = if (haveReleaseKey) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}

dependencies {
    // 后台值守的纯判断(AlertBell)用 JUnit 测:cd android && ./gradlew :app:testReleaseUnitTest(W17 外审)
    testImplementation("junit:junit:4.13.2")
}
