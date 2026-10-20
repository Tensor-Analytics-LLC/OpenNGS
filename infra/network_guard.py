"""OpenNGS-only endpoint and DNS egress controls; no NAT/firewall appliances."""
import json

def s3_policy(bucket,account):
    private=['arn:aws:s3:::'+bucket,'arn:aws:s3:::'+bucket+'/*',f'arn:aws:s3:::openngs-pro-{account}-*',f'arn:aws:s3:::openngs-pro-{account}-*/*',f'arn:aws:s3:::openngs-audit-{account}-us-east-1',f'arn:aws:s3:::openngs-audit-{account}-us-east-1/*']
    public=['arn:aws:s3:::'+b+'/*' for b in ['broad-references','giab','gnomad-public-us-east-1','1000genomes','al2023-repos-us-east-1-*']]
    return {'Version':'2012-10-17','Statement':[{'Sid':'OnlyOpenNGSPrivateStorage','Effect':'Allow','Principal':'*','Action':'s3:*','Resource':private,'Condition':{'StringEquals':{'aws:ResourceAccount':account}}},{'Sid':'OnlyReviewedECRLayers','Effect':'Allow','Principal':'*','Action':['s3:GetObject'],'Resource':'arn:aws:s3:::prod-us-east-1-starport-layer-bucket/*'},{'Sid':'PublicMetadataFromTrustedAppSubnetOnly','Effect':'Allow','Principal':'*','Action':['s3:GetObject','s3:ListBucket','s3:GetBucketLocation'],'Resource':public+['arn:aws:s3:::'+b for b in ['broad-references','giab','gnomad-public-us-east-1','1000genomes']],'Condition':{'IpAddress':{'aws:VpcSourceIp':'10.86.0.0/24'}}}]}

def template(vpc):
    tags=[{'Key':'Project','Value':'OpenNGS'}];ref=lambda n:{'Ref':n}
    domains=['*.amazonaws.com','*.api.aws','*.ec2.internal','*.compute.internal','*.amazonlinux.com','pypi.org','files.pythonhosted.org','*.fastly.net','ftp.ncbi.nlm.nih.gov','*.ncbi.nlm.nih.gov','ftp.ensembl.org','raw.githubusercontent.com','github.com','api.github.com','quay.io','registry-1.docker.io','auth.docker.io']
    resources={
      'Allowed':{'Type':'AWS::Route53Resolver::FirewallDomainList','Properties':{'Name':'openngs-approved-services','Domains':domains,'Tags':tags}},
      'All':{'Type':'AWS::Route53Resolver::FirewallDomainList','Properties':{'Name':'openngs-block-unapproved','Domains':['*'],'Tags':tags}},
      'Rules':{'Type':'AWS::Route53Resolver::FirewallRuleGroup','Properties':{'Name':'openngs-dns-egress','Tags':tags,'FirewallRules':[{'Action':'ALLOW','FirewallDomainListId':ref('Allowed'),'Priority':100},{'Action':'BLOCK','BlockResponse':'NXDOMAIN','FirewallDomainListId':ref('All'),'Priority':200}]}},
      'Association':{'Type':'AWS::Route53Resolver::FirewallRuleGroupAssociation','Properties':{'Name':'openngs-private-network','VpcId':vpc,'FirewallRuleGroupId':ref('Rules'),'Priority':101,'MutationProtection':'DISABLED','Tags':tags}}}
    return {'AWSTemplateFormatVersion':'2010-09-09','Description':'OpenNGS-only DNS allowlist; compute has no internet route; endpoint policies restrict S3 destinations','Resources':resources,'Outputs':{'Association':{'Value':ref('Association')}}}

if __name__=='__main__':
    import sys
    print(json.dumps(template(sys.argv[1]),indent=2))
