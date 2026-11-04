pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }
dependencyResolutionManagement { repositories { google(); mavenCentral() } }
plugins { id("com.android.application") version "9.1.0" apply false }
rootProject.name = "canvas-widget"
include(":app")
