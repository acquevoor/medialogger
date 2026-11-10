{ lib, stdenvNoCC, python3, qt6, makeWrapper, makeDesktopItem, copyDesktopItems }:
stdenvNoCC.mkDerivation {
  pname = "medialogger";
  version = "0.1.0";
  src = lib.fileset.toSource { root = ./.; fileset = lib.fileset.unions [ ./medialogger.py ./medialogger_app ]; };
  nativeBuildInputs = [ makeWrapper copyDesktopItems ];
  desktopItems = [
    (makeDesktopItem {
      name = "medialogger";
      desktopName = "Media Logger";
      comment = "Log games, anime, manga, music, films, light novels and visual novels";
      exec = "medialogger";
      icon = "accessories-text-editor";
      categories = [ "Utility" ];
    })
  ];
  installPhase = ''
    runHook preInstall
    install -Dm644 medialogger.py $out/share/medialogger/medialogger.py
    cp -r medialogger_app $out/share/medialogger/
    makeWrapper ${python3.withPackages (ps: [ ps.pyside6 ])}/bin/python $out/bin/medialogger \
      --add-flags $out/share/medialogger/medialogger.py \
      --prefix QT_PLUGIN_PATH : ${qt6.qtwayland}/${qt6.qtbase.qtPluginPrefix} \
      --prefix QT_PLUGIN_PATH : ${qt6.qtimageformats}/${qt6.qtbase.qtPluginPrefix}
    runHook postInstall
  '';
  meta = {
    description = "Local media log with Backloggd, AniList, RateYourMusic, Letterboxd, RanobeDB and VNDB integrations";
    homepage = "https://github.com/acquevoor/medialogger";
    mainProgram = "medialogger";
    platforms = lib.platforms.linux;
  };
}
