provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.control_plane_zone
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
  zone    = var.control_plane_zone
}
