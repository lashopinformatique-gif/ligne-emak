#!/bin/sh
# Construit ligne-emak_<version>_all.deb à partir des sources.
set -eu
cd "$(dirname "$0")"
VERSION=$(python3 -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('src/ligne_emak/__init__.py').read()).group(1))")
PKG=build/ligne-emak_${VERSION}_all
rm -rf build
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/ligne-emak/ligne_emak" "$PKG/usr/share/ligne-emak/icons" \
         "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/usr/share/doc/ligne-emak"

install -m 755 src/ligne-emak "$PKG/usr/bin/ligne-emak"
install -m 644 src/ligne_emak/*.py "$PKG/usr/share/ligne-emak/ligne_emak/"
install -m 644 data/style.css "$PKG/usr/share/ligne-emak/"
install -m 644 data/icons/*.svg "$PKG/usr/share/ligne-emak/icons/"
install -m 644 data/local.ligneemak.Softphone.svg "$PKG/usr/share/icons/hicolor/scalable/apps/"
install -m 644 data/local.ligneemak.Softphone.desktop "$PKG/usr/share/applications/"
install -m 644 README.md "$PKG/usr/share/doc/ligne-emak/README.md"
install -m 644 debian/copyright "$PKG/usr/share/doc/ligne-emak/copyright"
install -m 755 debian/postinst debian/prerm "$PKG/DEBIAN/"

SIZE=$(du -sk "$PKG/usr" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" debian/control > "$PKG/DEBIAN/control"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "build/ligne-emak_${VERSION}_all.deb"
echo "Paquet : build/ligne-emak_${VERSION}_all.deb"
