data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  prefix = "${var.name}-${var.environment}"
  zones  = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.prefix }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = local.prefix }
}

resource "aws_subnet" "public" {
  count = length(local.zones)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.zones[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)

  tags = { Name = "${local.prefix}-public-${count.index}" }
}

# Services and the database sit here: no route from the internet reaches them.
resource "aws_subnet" "private" {
  count = length(local.zones)

  vpc_id            = aws_vpc.main.id
  availability_zone = local.zones[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)

  tags = { Name = "${local.prefix}-private-${count.index}" }
}

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = { Name = "${local.prefix}-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]

  tags = { Name = local.prefix }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.prefix}-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = { Name = "${local.prefix}-private" }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count = length(aws_subnet.private)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Artifact traffic is large and sensitive; the gateway endpoint keeps it on the
# AWS network instead of the NAT gateway.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.prefix}-s3" }
}
