# ==========================================
# EKS (Elastic Kubernetes Service) CLUSTER
# ==========================================
# This file creates a managed Kubernetes cluster in AWS
# EKS handles the Kubernetes control plane (API server, etcd, scheduler)

# Step 1: Create EKS cluster using the official AWS EKS module
module "eks" {
  # Source: Official EKS module from Terraform registry
  # This module is maintained by AWS and includes best practices
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0" # Use version 20.x (latest stable)

  # Basic cluster configuration
  cluster_name    = var.cluster_name # Name: "expense-dev"
  cluster_version = var.eks_version  # Kubernetes version: "1.33"

  # Network access configuration
  # Allow public access to cluster API endpoint
  cluster_endpoint_public_access       = true
  # FIX: Added CIDR restriction variable — default is 0.0.0.0/0, restrict in prod
  cluster_endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs

  # PRODUCTION PATTERN: do NOT auto-grant admin to whoever happens to run
  # terraform apply. That's implicit, untied to a reviewed identity, and
  # silently gives cluster-admin to any CI/CD pipeline that applies this too.
  # Instead, every identity that needs cluster access gets a named,
  # explicit access_entries block below — scoped to exactly what that
  # identity needs, so access is auditable in code, not inferred from
  # "whoever ran the command."
  enable_cluster_creator_admin_permissions = false

  # Authentication mode: Use both API and ConfigMap for backwards compatibility
  # API mode is newer, ConfigMap is legacy but still supported
  authentication_mode = "API_AND_CONFIG_MAP"

  # Network configuration - where to place the cluster
  vpc_id     = module.vpc.vpc_id             # Use our VPC created in vpc.tf
  subnet_ids = module.vpc.private_subnet_ids # Place cluster in private subnets (more secure)

  # Worker node configuration - where pods will run
  eks_managed_node_groups = local.eks_managed_node_groups # Defined in local.tf

  # Step 2: Configure AWS user/role access to EKS cluster
  # Every principal that needs cluster access is listed here explicitly,
  # by name, with a scoped policy. This is the auditable production pattern —
  # nobody gets access just by being the one who ran `terraform apply`.
  access_entries = {
    # The human/CI identity actually running Terraform still needs to be
    # listed explicitly now that enable_cluster_creator_admin_permissions
    # is false — otherwise nobody, including you, can reach the cluster.
    #
    # NOTE: EKS access entries require the underlying IAM user/role ARN,
    # NOT an assumed-role session ARN. data.aws_caller_identity.current.arn
    # returns arn:aws:sts::<acct>:assumed-role/<role>/<session> when you're
    # running as an assumed role (common for CI, or an IAM user with an
    # assumed admin role) — EKS will reject that format. If you're running
    # Terraform as a plain IAM user, current.arn is already correct as-is.
    # If you're running as an assumed role, replace this with the literal
    # role ARN instead, e.g.:
    #   principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/<your-actual-role-name>"
    terraform_runner = {
      principal_arn = data.aws_caller_identity.current.arn
      policy_associations = {
        admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }

    # A separate named admin identity (e.g. a break-glass/admin IAM user),
    # kept distinct from the Terraform runner so access isn't tied to one
    # credential only.
    terraform_admin = {
      principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/terraform-admin"
      policy_associations = {
        admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }

  }

  # Step 3: Configure security groups for worker nodes
  create_node_security_group = true
  node_security_group_additional_rules = {
    # Allow nodes to communicate with each other on all ports
    # This is required for pod-to-pod networking and CNI
    ingress_self_all = {
      description = "Node to node all ports/protocols - required for pod networking"
      protocol    = "-1"      # -1 means all protocols (TCP, UDP, ICMP)
      from_port   = 0         # All ports
      to_port     = 0         # All ports
      type        = "ingress" # Incoming traffic
      self        = true      # From same security group (node-to-node)
    }
  }

  # Step 4: Apply consistent tags to all EKS resources
  tags = merge(var.common_tags, {
    Name = var.cluster_name # Display name in AWS console
    Type = "EKS-Cluster"    # Resource type identifier
  })
}

# What this creates:
# 1. EKS Control Plane (managed by AWS)
#    - Kubernetes API server
#    - etcd database
#    - Scheduler and controller manager
# 2. Worker Node Groups (EC2 instances)
#    - Auto Scaling Groups
#    - Launch Templates
#    - Security Groups
# 3. OIDC Provider (for IAM roles for service accounts)
# 4. Cluster security groups and networking
# 5. Access entries for user authentication

# After this runs, you can:
# - Connect with: aws eks update-kubeconfig --name expense-dev
# - Deploy applications using kubectl
# - Use Helm charts for complex applications
# - Set up monitoring and logging
