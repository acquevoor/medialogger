{
  description = "Local media log with Backloggd, AniList, RateYourMusic, Letterboxd, RanobeDB and VNDB integrations";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      forAll = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (s: f nixpkgs.legacyPackages.${s});
    in
    {
      # ponytail: no package.nix, home/medialogger.nix in ~/flake runs the checkout; add one when publishing
      checks = forAll (pkgs: {
        selftest = pkgs.runCommand "medialogger-selftest" { } ''
          export HOME=$TMPDIR
          ${pkgs.python3}/bin/python ${self}/medialogger.py --selftest && touch $out
        '';
      });
    };
}
