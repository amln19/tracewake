resource "aws_security_group" "public_lb" {
  name        = "${local.prefix}-public-lb"
  description = "Public entry point for tenant API traffic"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.prefix}-public-lb" }
}

resource "aws_security_group_rule" "public_lb_https" {
  type              = "ingress"
  security_group_id = aws_security_group.public_lb.id
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = var.allowed_tenant_cidrs
  description       = "Tenant API over TLS"
}

resource "aws_security_group_rule" "public_lb_http" {
  type              = "ingress"
  security_group_id = aws_security_group.public_lb.id
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = var.allowed_tenant_cidrs
  description       = "Tenant API redirect to TLS"
}

# The worker API is reachable only from worker tasks through an internal load
# balancer; it is never published to the internet.
resource "aws_security_group" "internal_lb" {
  name        = "${local.prefix}-internal-lb"
  description = "Private worker API entry point"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.prefix}-internal-lb" }
}

resource "aws_security_group" "control_plane" {
  name        = "${local.prefix}-control-plane"
  description = "Go control-plane tasks"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.prefix}-control-plane" }
}

resource "aws_security_group" "worker" {
  name        = "${local.prefix}-worker"
  description = "Python worker tasks"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.prefix}-worker" }
}

resource "aws_security_group" "database" {
  name        = "${local.prefix}-database"
  description = "Authoritative hosted state"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.prefix}-database" }
}

resource "aws_security_group_rule" "control_plane_from_public_lb" {
  type                     = "ingress"
  security_group_id        = aws_security_group.control_plane.id
  source_security_group_id = aws_security_group.public_lb.id
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  description              = "Tenant API"
}

resource "aws_security_group_rule" "control_plane_from_internal_lb" {
  type                     = "ingress"
  security_group_id        = aws_security_group.control_plane.id
  source_security_group_id = aws_security_group.internal_lb.id
  from_port                = 8081
  to_port                  = 8081
  protocol                 = "tcp"
  description              = "Worker API"
}

resource "aws_security_group_rule" "internal_lb_from_worker" {
  type                     = "ingress"
  security_group_id        = aws_security_group.internal_lb.id
  source_security_group_id = aws_security_group.worker.id
  from_port                = 8081
  to_port                  = 8081
  protocol                 = "tcp"
  description              = "Workers only"
}

resource "aws_security_group_rule" "database_from_control_plane" {
  type                     = "ingress"
  security_group_id        = aws_security_group.database.id
  source_security_group_id = aws_security_group.control_plane.id
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  description              = "Only the control plane writes hosted state"
}
