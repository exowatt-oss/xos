#!/usr/bin/env bash
# Build the Xos TV APK with the plain Android SDK tools (no Gradle).
# Needs: a JDK, and ANDROID_HOME with build-tools;35.0.0 + platforms;android-34
#   brew install --cask android-commandlinetools android-platform-tools
#   sdkmanager --sdk_root="$HOME/Library/Android/sdk" "build-tools;35.0.0" "platforms;android-34"
# Output: firetv/build/xos-tv.apk (signed with the standard local debug key).
set -euo pipefail

cd "$(dirname "$0")"
SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
BT="$SDK/build-tools/35.0.0"
JAR="$SDK/platforms/android-34/android.jar"
OUT=build
# macOS ships a /usr/bin/java stub that fails without a JDK; prefer Homebrew's if present.
if [ -z "${JAVA_HOME:-}" ] && [ -x /opt/homebrew/opt/openjdk/bin/javac ]; then
  export JAVA_HOME=/opt/homebrew/opt/openjdk
fi
[ -n "${JAVA_HOME:-}" ] && export PATH="$JAVA_HOME/bin:$PATH"
KEYSTORE="${XOS_TV_KEYSTORE:-$HOME/.android/debug.keystore}"

[ -f "$JAR" ] || { echo "Missing $JAR — see the header of this script." >&2; exit 1; }
rm -rf "$OUT" && mkdir -p "$OUT/res" "$OUT/gen" "$OUT/classes"

"$BT/aapt2" compile --dir res -o "$OUT/res"
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml \
  --min-sdk-version 22 --target-sdk-version 34 \
  --java "$OUT/gen" -o "$OUT/unsigned.apk" "$OUT"/res/*.flat

javac --release 11 -nowarn -Xlint:-options -classpath "$JAR" -d "$OUT/classes" \
  $(find src "$OUT/gen" -name '*.java')
"$BT/d8" --min-api 22 --lib "$JAR" --output "$OUT" $(find "$OUT/classes" -name '*.class')
(cd "$OUT" && zip -q unsigned.apk classes.dex)

"$BT/zipalign" -f -p 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
if [ ! -f "$KEYSTORE" ]; then
  mkdir -p "$(dirname "$KEYSTORE")"
  keytool -genkeypair -keystore "$KEYSTORE" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null
fi
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$OUT/xos-tv.apk" "$OUT/aligned.apk"
"$BT/apksigner" verify "$OUT/xos-tv.apk"
echo "Built $(pwd)/$OUT/xos-tv.apk ($(du -h "$OUT/xos-tv.apk" | cut -f1))"
