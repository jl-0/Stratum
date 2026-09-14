# One image, two entrypoints (spec 08 section 2). Tags are IMMUTABLE because deployment/
# pins by digest - a mutable tag would make a deploy unreproducible.
resource "aws_ecr_repository" "stratum" {
  name                 = "stratum-${var.deployment}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "stratum" {
  repository = aws_ecr_repository.stratum.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}
