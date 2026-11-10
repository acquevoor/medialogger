{
  description = "Local media log with Backloggd, AniList, RateYourMusic, Letterboxd, RanobeDB and VNDB integrations";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      forAll = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAll (pkgs: { default = pkgs.callPackage ./package.nix { }; });
      apps = forAll (pkgs: {
        default = { type = "app"; program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/medialogger"; };
      });
      checks = forAll (pkgs: {
        selftest = pkgs.runCommand "medialogger-selftest" { } ''
          export HOME=$TMPDIR
          ${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/medialogger --selftest && touch $out
        '';
      });
    };
}
