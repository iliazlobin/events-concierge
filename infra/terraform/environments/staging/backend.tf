terraform {
  # Supply bucket and prefix through -backend-config. The state bucket is a separately bootstrapped
  # security boundary so this environment never contains a self-destroying backend resource.
  backend "gcs" {}
}
