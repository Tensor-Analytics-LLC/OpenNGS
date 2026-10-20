"""Reproducible single-AZ control plane, private origin and private database.

No NAT, ALB, EKS, Redis, commitments, or idle scientific servers. IPv6 outbound
only on the trusted API host; isolated scientific subnet has only an S3 route.
"""
import json

def template(bucket,kms,release):
    ref=lambda x:{'Ref':x}
    att=lambda x,y:{'Fn::GetAtt':[x,y]}
    tags=[{'Key':'Project','Value':'OpenNGS'}]
    R={}
    def add(name,kind,properties,**extra):R[name]={'Type':kind,'Properties':properties,**extra}
    add('VPC','AWS::EC2::VPC',{'CidrBlock':'10.86.0.0/16','EnableDnsSupport':True,'EnableDnsHostnames':True,'Tags':tags})
    add('IPv6','AWS::EC2::VPCCidrBlock',{'VpcId':ref('VPC'),'AmazonProvidedIpv6CidrBlock':True})
    add('Gateway','AWS::EC2::InternetGateway',{'Tags':tags})
    add('Attach','AWS::EC2::VPCGatewayAttachment',{'VpcId':ref('VPC'),'InternetGatewayId':ref('Gateway')})
    add('Egress6','AWS::EC2::EgressOnlyInternetGateway',{'VpcId':ref('VPC')})
    for name,cidr,zone in [('AppSubnet','10.86.0.0/24','us-east-1a'),('WorkerSubnet','10.86.1.0/24','us-east-1a'),('DBSubnetA','10.86.2.0/24','us-east-1a'),('DBSubnetB','10.86.3.0/24','us-east-1b')]:
        props={'VpcId':ref('VPC'),'CidrBlock':cidr,'AvailabilityZone':zone,'MapPublicIpOnLaunch':False,'Tags':tags}
        if name=='AppSubnet':props.update(AssignIpv6AddressOnCreation=True,Ipv6CidrBlock={'Fn::Select':[0,{'Fn::Cidr':[{'Fn::Select':[0,att('VPC','Ipv6CidrBlocks')]},1,64]}]})
        add(name,'AWS::EC2::Subnet',props,**({'DependsOn':'IPv6'} if name=='AppSubnet' else {}))
    add('AppRoutes','AWS::EC2::RouteTable',{'VpcId':ref('VPC'),'Tags':tags})
    add('WorkerRoutes','AWS::EC2::RouteTable',{'VpcId':ref('VPC'),'Tags':tags})
    add('AppRoute6','AWS::EC2::Route',{'RouteTableId':ref('AppRoutes'),'DestinationIpv6CidrBlock':'::/0','EgressOnlyInternetGatewayId':ref('Egress6')})
    for subnet,routes in [('AppSubnet','AppRoutes'),('WorkerSubnet','WorkerRoutes')]:add(subnet+'Routes','AWS::EC2::SubnetRouteTableAssociation',{'SubnetId':ref(subnet),'RouteTableId':ref(routes)})
    add('S3Endpoint','AWS::EC2::VPCEndpoint',{'VpcId':ref('VPC'),'RouteTableIds':[ref('AppRoutes'),ref('WorkerRoutes')],'ServiceName':'com.amazonaws.us-east-1.s3','VpcEndpointType':'Gateway'})
    add('AppSecurity','AWS::EC2::SecurityGroup',{'VpcId':ref('VPC'),'GroupDescription':'OpenNGS private origin; no public ingress','SecurityGroupIngress':[{'IpProtocol':'tcp','FromPort':8080,'ToPort':8080,'CidrIp':'10.86.0.0/24'}],'SecurityGroupEgress':[{'IpProtocol':'tcp','FromPort':443,'ToPort':443,'CidrIpv6':'::/0'},{'IpProtocol':'tcp','FromPort':443,'ToPort':443,'CidrIp':'0.0.0.0/0'},{'IpProtocol':'tcp','FromPort':5432,'ToPort':5432,'CidrIp':'10.86.2.0/23'}],'Tags':tags})
    add('DBSecurity','AWS::EC2::SecurityGroup',{'VpcId':ref('VPC'),'GroupDescription':'PostgreSQL from OpenNGS API only','SecurityGroupIngress':[{'IpProtocol':'tcp','FromPort':5432,'ToPort':5432,'SourceSecurityGroupId':ref('AppSecurity')}],'SecurityGroupEgress':[{'IpProtocol':'tcp','FromPort':9,'ToPort':9,'CidrIp':'127.0.0.1/32'}],'Tags':tags})
    add('WorkerSecurity','AWS::EC2::SecurityGroup',{'VpcId':ref('VPC'),'GroupDescription':'Single-job VM: no ingress, S3-only route, no IPv6','SecurityGroupEgress':[{'IpProtocol':'tcp','FromPort':443,'ToPort':443,'CidrIp':'0.0.0.0/0'}],'Tags':tags})
    add('EndpointSecurity','AWS::EC2::SecurityGroup',{'VpcId':ref('VPC'),'GroupDescription':'Secrets endpoint from trusted API only','SecurityGroupIngress':[{'IpProtocol':'tcp','FromPort':443,'ToPort':443,'SourceSecurityGroupId':ref('AppSecurity')}],'SecurityGroupEgress':[{'IpProtocol':'tcp','FromPort':9,'ToPort':9,'CidrIp':'127.0.0.1/32'}],'Tags':tags})
    add('SecretsEndpoint','AWS::EC2::VPCEndpoint',{'VpcId':ref('VPC'),'SubnetIds':[ref('AppSubnet')],'SecurityGroupIds':[ref('EndpointSecurity')],'ServiceName':'com.amazonaws.us-east-1.secretsmanager','VpcEndpointType':'Interface','PrivateDnsEnabled':True})
    add('DBSubnets','AWS::RDS::DBSubnetGroup',{'DBSubnetGroupDescription':'OpenNGS private database subnets','SubnetIds':[ref('DBSubnetA'),ref('DBSubnetB')],'Tags':tags})
    add('Database','AWS::RDS::DBInstance',{'DBInstanceIdentifier':'openngs-lean','DBName':'openngs','Engine':'postgres','EngineVersion':'17','DBInstanceClass':'db.t4g.micro','AllocatedStorage':'20','StorageType':'gp3','StorageEncrypted':True,'KmsKeyId':kms,'MasterUsername':'openngs','ManageMasterUserPassword':True,'DBSubnetGroupName':ref('DBSubnets'),'VPCSecurityGroups':[ref('DBSecurity')],'PubliclyAccessible':False,'MultiAZ':False,'BackupRetentionPeriod':7,'AutoMinorVersionUpgrade':True,'DeletionProtection':True,'CopyTagsToSnapshot':True,'Tags':tags},DeletionPolicy='Retain',UpdateReplacePolicy='Retain')
    assume={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'ec2.amazonaws.com'},'Action':'sts:AssumeRole'}]}
    policy={'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:ListBucket','s3:GetBucketLocation','s3:ListBucketMultipartUploads'],'Resource':'arn:aws:s3:::'+bucket},
        {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':['arn:aws:s3:::'+bucket+'/tenants/*','arn:aws:s3:::'+bucket+'/operations/*']},
        {'Effect':'Allow','Action':['s3:PutObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],'Resource':['arn:aws:s3:::'+bucket+'/tenants/*','arn:aws:s3:::'+bucket+'/operations/checkpoints/*']},
        {'Effect':'Allow','Action':['kms:Decrypt','kms:Encrypt','kms:GenerateDataKey'],'Resource':kms},
        {'Effect':'Allow','Action':['secretsmanager:GetSecretValue'],'Resource':att('Database','MasterUserSecret.SecretArn')},
        {'Effect':'Allow','Action':['ec2:DescribeInstances','ec2:DescribeInstanceStatus','ec2:GetConsoleOutput'],'Resource':'*'},
        {'Effect':'Allow','Action':['ec2:TerminateInstances','ec2:CreateTags'],'Resource':'arn:aws:ec2:us-east-1:*:instance/*','Condition':{'StringEquals':{'ec2:ResourceTag/Project':'OpenNGS-worker'}}}
    ]}
    add('AppRole','AWS::IAM::Role',{'AssumeRolePolicyDocument':assume,'ManagedPolicyArns':['arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'],'Policies':[{'PolicyName':'OpenNGSApplication','PolicyDocument':policy}],'Tags':tags})
    add('InstanceProfile','AWS::IAM::InstanceProfile',{'Roles':[ref('AppRole')]})
    bootstrap='#!/bin/bash\nset -euo pipefail\numask 077\nexport AWS_DEFAULT_REGION=us-east-1 AWS_USE_DUALSTACK_ENDPOINT=true\n'+f'export NGS_BUCKET={bucket}\nexport NGS_RELEASE={release}\nexport NGS_KMS_KEY={kms}\n'+"export NGS_DB_SECRET='${Database.MasterUserSecret.SecretArn}'\nexport NGS_DB_HOST='${Database.Endpoint.Address}'\nexport NGS_ORIGIN_TOKEN='${OriginToken}'\n"+f"aws s3 cp s3://{bucket}/{release}/bootstrap.sh /tmp/openngs-bootstrap.sh\nbash /tmp/openngs-bootstrap.sh\n"
    add('Host','AWS::EC2::Instance',{'ImageId':ref('ImageId'),'InstanceType':'t4g.medium','CreditSpecification':{'CPUCredits':'standard'},'IamInstanceProfile':ref('InstanceProfile'),'SubnetId':ref('AppSubnet'),'SecurityGroupIds':[ref('AppSecurity')],'MetadataOptions':{'HttpTokens':'required','HttpPutResponseHopLimit':1},'BlockDeviceMappings':[{'DeviceName':'/dev/xvda','Ebs':{'VolumeSize':30,'VolumeType':'gp3','Encrypted':True,'DeleteOnTermination':True}}],'UserData':{'Fn::Base64':{'Fn::Sub':bootstrap}},'Tags':tags+[{'Key':'Name','Value':'OpenNGS control plane'}]},DependsOn=['AppRoute6','S3Endpoint','SecretsEndpoint','Database'])
    add('PrivateOrigin','AWS::CloudFront::VpcOrigin',{'VpcOriginEndpointConfig':{'Arn':{'Fn::Sub':'arn:aws:ec2:${AWS::Region}:${AWS::AccountId}:instance/${Host}'},'Name':'OpenNGS-private-api','HTTPPort':8080,'HTTPSPort':443,'OriginProtocolPolicy':'http-only','OriginSSLProtocols':['TLSv1.2']}})
    visibility=lambda name:{'CloudWatchMetricsEnabled':True,'SampledRequestsEnabled':False,'MetricName':name}
    add('Firewall','AWS::WAFv2::WebACL',{'Name':'OpenNGS-edge','Scope':'CLOUDFRONT','DefaultAction':{'Allow':{}},'VisibilityConfig':visibility('OpenNGS-edge'),'Rules':[{'Name':'IPRate','Priority':0,'Action':{'Block':{'CustomResponse':{'ResponseCode':429}}},'Statement':{'RateBasedStatement':{'Limit':10000,'AggregateKeyType':'IP'}},'VisibilityConfig':visibility('OpenNGS-IP-rate')},{'Name':'CommonThreats','Priority':1,'OverrideAction':{'None':{}},'Statement':{'ManagedRuleGroupStatement':{'VendorName':'AWS','Name':'AWSManagedRulesCommonRuleSet','RuleActionOverrides':[{'Name':'SizeRestrictions_BODY','ActionToUse':{'Count':{}}}]}},'VisibilityConfig':visibility('OpenNGS-common-threats')}],'Tags':tags})
    add('Distribution','AWS::CloudFront::Distribution',{'DistributionConfig':{'Enabled':True,'Comment':'OpenNGS by Tensor Analytics; private research origin','HttpVersion':'http2and3','IPV6Enabled':True,'PriceClass':'PriceClass_All','WebACLId':att('Firewall','Arn'),'Origins':[{'Id':'api','DomainName':att('Host','PrivateDnsName'),'VpcOriginConfig':{'VpcOriginId':att('PrivateOrigin','Id'),'OriginReadTimeout':60,'OriginKeepaliveTimeout':5},'OriginCustomHeaders':[{'HeaderName':'X-NGS-Origin','HeaderValue':ref('OriginToken')}]}],'DefaultCacheBehavior':{'TargetOriginId':'api','ViewerProtocolPolicy':'redirect-to-https','AllowedMethods':['GET','HEAD','OPTIONS','PUT','POST','PATCH','DELETE'],'CachedMethods':['GET','HEAD'],'Compress':True,'CachePolicyId':'4135ea2d-6df8-44a3-9df3-4b5a84be39ad','OriginRequestPolicyId':'216adef6-5c7f-47e4-b989-5492eafa07d3'},'CustomErrorResponses':[{'ErrorCode':e,'ErrorCachingMinTTL':0} for e in [400,403,404,500,502,503,504]],'ViewerCertificate':{'CloudFrontDefaultCertificate':True}},'Tags':tags})
    return {'AWSTemplateFormatVersion':'2010-09-09','Description':'OpenNGS budgeted single-AZ research platform; private origin, private RDS, zero-idle scientific subnet','Parameters':{'ImageId':{'Type':'AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>','Default':'/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64'},'OriginToken':{'Type':'String','NoEcho':True,'MinLength':40}},'Resources':R,'Outputs':{'URL':{'Value':{'Fn::Sub':'https://${Distribution.DomainName}'}},'HostId':{'Value':ref('Host')},'DatabaseEndpoint':{'Value':att('Database','Endpoint.Address')},'WorkerSubnet':{'Value':ref('WorkerSubnet')},'WorkerSecurity':{'Value':ref('WorkerSecurity')},'AppRole':{'Value':ref('AppRole')}}}

if __name__=='__main__':
    import sys
    print(json.dumps(template(*sys.argv[1:]),indent=2))
