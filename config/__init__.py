'''
Tags: Main

config — Four-level configuration cascade.

DevQ core defaults → provider preferred_config() → global user JSON →
per-device user JSON, with per-key provenance tracking surfaced by
qconfig. (`shots` has one further per-job tier above the cascade.)
'''