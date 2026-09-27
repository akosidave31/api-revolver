plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.akosidave.revolver"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.akosidave.revolver"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.4.0"
        ndk { abiFilters += listOf("arm64-v8a") }   // Python 3.12+ is 64-bit only
    }

    // Optional stable signing (so updates install over the old app and keep
    // your keys). Used only when CI provides android/keystore.jks.
    val ks = rootProject.file("keystore.jks")
    signingConfigs {
        create("stable") {
            if (ks.exists()) {
                storeFile = ks
                storePassword = System.getenv("KS_PASS") ?: ""
                keyAlias = System.getenv("KS_ALIAS") ?: "revolver"
                keyPassword = System.getenv("KS_PASS") ?: ""
            }
        }
    }
    buildTypes {
        getByName("debug") {
            if (ks.exists()) signingConfig = signingConfigs.getByName("stable")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        pip { install("requests") }
    }
}
