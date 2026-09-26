bucket         = "shivakrishna-tf-state-prod"
key            = "prod/terraform.tfstate"
region         = "us-east-1"
dynamodb_table = "terraform-state-lock-prod"
encrypt        = true