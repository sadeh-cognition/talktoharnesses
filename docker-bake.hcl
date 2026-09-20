// Bake definition for the tth-<kind> split service images.
//
//   docker buildx bake            # all seven, in parallel (run from the repo root)
//   docker buildx bake claude     # one kind
//
// deploy/build-splits.sh wraps this and fills in TAG/HOST_UID/HOST_GID; the proxy's
// on-demand build (remote/docker_ops.build_image) runs the same target. The file
// lives at the repo root so every context path stays below the working directory
// (bake refuses parent-directory contexts without --allow=fs.read).

variable "TAG" { default = "latest" }
variable "HOST_UID" { default = "1000" }
variable "HOST_GID" { default = "1000" }

group "default" {
  targets = ["split", "gateway"]
}

target "split" {
  name     = kind
  matrix   = { kind = ["grok", "cursor", "codex", "claude", "opencode", "prime-agent", "muse"] }
  context  = "tth-${kind}"
  tags     = ["tth-${kind}:${TAG}"]
  args     = { UID = HOST_UID, GID = HOST_GID }
  contexts = { tth_types = "tth-types" }
}


target "gateway" {
  context = "."
  dockerfile = "deploy/gateway.Dockerfile"
  tags = ["tth-policy-gateway:${TAG}"]
  args = { UID = HOST_UID, GID = HOST_GID }
}
