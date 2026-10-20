"""Fixed tenant stack. Scientific capacity scales to zero and has a hard ceiling.

The application may select parameters, never submit a replacement template.
Only reviewed core-runtime tools run here; GPUs/VM-scale scratch are separate
profiles and remain unavailable until published and validated.
"""
import copy
import json

def template(subnet,worker_sg,execution_role,image,repository_arn,shared_bucket,kms,boundary,origin,alert_topic,extension=None,secondary=None,deepvariant=None,vep=None,formats=None):
    if '@sha256:' not in image:raise ValueError('Pin the reviewed runtime digest')
    ref=lambda x:{'Ref':x};sub=lambda x:{'Fn::Sub':x};att=lambda x,k:{'Fn::GetAtt':[x,k]}
    tags=[{'Key':'Project','Value':'OpenNGS-Pro'},{'Key':'TenantId','Value':ref('TenantId')}]
    R={}
    def add(n,t,p,**extra):R[n]={'Type':t,'Properties':p,**extra}
    add('Data','AWS::S3::Bucket',{'BucketName':sub('openngs-pro-${AWS::AccountId}-${StorageId}'),'PublicAccessBlockConfiguration':{'BlockPublicAcls':True,'IgnorePublicAcls':True,'BlockPublicPolicy':True,'RestrictPublicBuckets':True},'OwnershipControls':{'Rules':[{'ObjectOwnership':'BucketOwnerEnforced'}]},'BucketEncryption':{'ServerSideEncryptionConfiguration':[{'ServerSideEncryptionByDefault':{'SSEAlgorithm':'aws:kms','KMSMasterKeyID':kms},'BucketKeyEnabled':True}]},'VersioningConfiguration':{'Status':'Enabled'},'CorsConfiguration':{'CorsRules':[{'AllowedOrigins':[origin],'AllowedMethods':['PUT','HEAD'],'AllowedHeaders':['content-type','content-length','x-amz-*'],'ExposedHeaders':['ETag'],'MaxAge':300}]},'LifecycleConfiguration':{'Rules':[{'Id':'AbandonedParts','Status':'Enabled','AbortIncompleteMultipartUpload':{'DaysAfterInitiation':7}}]},'Tags':tags},DeletionPolicy='Retain',UpdateReplacePolicy='Retain')
    add('DataPolicy','AWS::S3::BucketPolicy',{'Bucket':ref('Data'),'PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[att('Data','Arn'),sub('${Data.Arn}/*')],'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]}})
    assume={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'ecs-tasks.amazonaws.com'},'Action':'sts:AssumeRole','Condition':{'StringEquals':{'aws:SourceAccount':ref('AWS::AccountId')},'ArnLike':{'aws:SourceArn':sub('arn:aws:ecs:us-east-1:${AWS::AccountId}:*')}}}]}
    add('TaskRole','AWS::IAM::Role',{'Path':'/openngs/pro/','PermissionsBoundary':boundary,'AssumeRolePolicyDocument':assume,'Tags':tags,'Policies':[{'PolicyName':'OnlyThisTenantAndImmutablePublicReferences','PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':sub('${Data.Arn}/tenants/${TenantId}/*')},{'Effect':'Allow','Action':['s3:PutObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],'Resource':sub('${Data.Arn}/tenants/${TenantId}/runs/*')},{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+shared_bucket+'/operations/checkpoints/references/*'},{'Effect':'Allow','Action':['kms:Decrypt','kms:GenerateDataKey'],'Resource':kms,'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}]}}]})
    assume['Statement'].append({'Effect':'Allow','Principal':{'Service':'ec2.amazonaws.com'},'Action':'sts:AssumeRole'})
    R['TaskRole']['Properties']['Policies'][0]['PolicyDocument']['Statement'].append(
        {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],
         'Resource':sub('arn:aws:s3:::'+shared_bucket+'/tenants/${TenantId}/datasets/*')})
    add('InstanceProfile','AWS::IAM::InstanceProfile',{'Path':'/openngs/pro/','Roles':[ref('TaskRole')]})
    add('Logs','AWS::Logs::LogGroup',{'LogGroupName':sub('/openngs/pro/${TenantId}'),'RetentionInDays':7,'Tags':tags})
    add('Compute','AWS::Batch::ComputeEnvironment',{'ComputeEnvironmentName':sub('openngs-pro-${TenantId}'),'Type':'MANAGED','State':'ENABLED','ComputeResources':{'Type':'FARGATE_SPOT','MaxvCpus':ref('FleetVCPU'),'Subnets':[subnet],'SecurityGroupIds':[worker_sg]},'Tags':{'Project':'OpenNGS-Pro','TenantId':ref('TenantId')}})
    add('Queue','AWS::Batch::JobQueue',{'JobQueueName':sub('openngs-pro-${TenantId}'),'State':'ENABLED','Priority':1,'ComputeEnvironmentOrder':[{'Order':1,'ComputeEnvironment':ref('Compute')}],'Tags':{'Project':'OpenNGS-Pro','TenantId':ref('TenantId')}})
    add('Definition','AWS::Batch::JobDefinition',{'JobDefinitionName':sub('openngs-pro-${TenantId}'),'Type':'container','PlatformCapabilities':['FARGATE'],'ContainerProperties':{'Image':image,'Command':['python','/opt/studio/cloud_worker.py'],'ResourceRequirements':[{'Type':'VCPU','Value':'2'},{'Type':'MEMORY','Value':'4096'}],'JobRoleArn':att('TaskRole','Arn'),'ExecutionRoleArn':execution_role,'ReadonlyRootFilesystem':True,'User':'1000:1000','EphemeralStorage':{'SizeInGiB':200},'NetworkConfiguration':{'AssignPublicIp':'DISABLED'},'Volumes':[{'Name':n} for n in ['work','inputs','tmp']],'MountPoints':[{'SourceVolume':n,'ContainerPath':'/'+n,'ReadOnly':False} for n in ['work','inputs','tmp']],'LogConfiguration':{'LogDriver':'awslogs','Options':{'awslogs-group':ref('Logs'),'awslogs-region':'us-east-1','awslogs-stream-prefix':'research'}}},'RetryStrategy':{'Attempts':1},'Timeout':{'AttemptDurationSeconds':172800},'Tags':{'Project':'OpenNGS-Pro','TenantId':ref('TenantId'),'NGSWorkerContract':'1','NGSIsolationProfile':'Fargate-tenant-role-v1'}})
    add('Budget','AWS::Budgets::Budget',{'Budget':{'BudgetName':sub('openngs-pro-${TenantId}'),'BudgetType':'COST','TimeUnit':'MONTHLY','BudgetLimit':{'Amount':ref('MonthlyBudget'),'Unit':'USD'},'CostFilters':{'TagKeyValue':[sub('TenantId$${TenantId}')]},'CostTypes':{'IncludeTax':True,'IncludeCredit':False,'IncludeRefund':False}},'NotificationsWithSubscribers':[{'Notification':{'NotificationType':kind,'ComparisonOperator':'GREATER_THAN','Threshold':90,'ThresholdType':'PERCENTAGE'},'Subscribers':[{'SubscriptionType':'SNS','Address':alert_topic}]} for kind in ['ACTUAL','FORECASTED']]})
    outputs={'Bucket':{'Value':ref('Data')},'Queue':{'Value':ref('Queue')},'Definition':{'Value':ref('Definition')},'TaskRole':{'Value':att('TaskRole','Arn')},'InstanceProfile':{'Value':att('InstanceProfile','Arn')},'Image':{'Value':image},'ScratchMiB':{'Value':'184320'}}
    if extension:
        from backend.runtime_catalog import validate_release
        release=validate_release(extension)
        properties=copy.deepcopy(R['Definition']['Properties'])
        properties['JobDefinitionName']=sub('openngs-pro-${TenantId}-extended')
        properties['ContainerProperties']['Image']=release['image']
        properties['Tags']['NGSRuntimeFamily']='extended-cpu-v1'
        add('ExtendedDefinition','AWS::Batch::JobDefinition',properties)
        outputs.update(ExtendedImage={'Value':release['image']},ExtendedDefinition={'Value':ref('ExtendedDefinition')},
                       ExtendedTools={'Value':json.dumps(release['tools'])},ExtendedSource={'Value':release['source_sha256']})
        if release.get('refreshed_core_tools'):
            outputs['RefreshedCoreTools']={'Value':json.dumps(release['refreshed_core_tools'])}
        if release.get('adjunct_tools'):
            outputs['AdjunctTools']={'Value':json.dumps(release['adjunct_tools'])}
        if release.get('portable_tools'):
            outputs['PortableTools']={'Value':json.dumps(release['portable_tools'])}
    if secondary:
        from backend.secondary_runtime import validate_release as validate_secondary
        release=validate_secondary(secondary)
        if release['tools']:
            properties=copy.deepcopy(R['Definition']['Properties'])
            properties['JobDefinitionName']=sub('openngs-pro-${TenantId}-secondary')
            properties['ContainerProperties']['Image']=release['image']
            properties['Tags']['NGSRuntimeFamily']='secondary-cpu-v1'
            add('SecondaryDefinition','AWS::Batch::JobDefinition',properties)
            outputs.update(SecondaryImage={'Value':release['image']},SecondaryDefinition={'Value':ref('SecondaryDefinition')},
                           SecondaryTools={'Value':json.dumps(release['tools'])},SecondarySource={'Value':release['source_sha256']})
    if deepvariant:
        from backend.deepvariant_runtime import validate_release as validate_deepvariant
        release=validate_deepvariant(deepvariant)
        properties=copy.deepcopy(R['Definition']['Properties'])
        properties['JobDefinitionName']=sub('openngs-pro-${TenantId}-deepvariant')
        properties['ContainerProperties']['Image']=release['image']
        properties['ContainerProperties']['Command']=['/usr/bin/python3','/opt/studio/cloud_worker.py']
        properties['Tags']['NGSRuntimeFamily']='deepvariant-cpu-v1'
        add('DeepVariantDefinition','AWS::Batch::JobDefinition',properties)
        outputs.update(DeepVariantImage={'Value':release['image']},DeepVariantDefinition={'Value':ref('DeepVariantDefinition')},
                       DeepVariantTools={'Value':json.dumps(release['tools'])},DeepVariantModels={'Value':json.dumps(release['models'])},
                       DeepVariantSource={'Value':release['source_sha256']})
    if vep:
        from backend.vep_runtime import validate_release as validate_vep
        release=validate_vep(vep)
        properties=copy.deepcopy(R['Definition']['Properties'])
        properties['JobDefinitionName']=sub('openngs-pro-${TenantId}-vep')
        properties['ContainerProperties']['Image']=release['image']
        properties['ContainerProperties']['Command']=['/usr/bin/python3','/opt/studio/cloud_worker.py']
        properties['Tags']['NGSRuntimeFamily']='ensembl-vep-offline-v1'
        add('VEPDefinition','AWS::Batch::JobDefinition',properties)
        outputs.update(VEPImage={'Value':release['image']},VEPDefinition={'Value':ref('VEPDefinition')},
                       VEPTools={'Value':json.dumps(release['tools'])},VEPSource={'Value':release['source_sha256']},
                       VEPCacheVersion={'Value':str(release['cache_version'])},VEPAssembly={'Value':release['assembly']},
                       VEPCacheManifest={'Value':release['cache_manifest_sha256']})
    if formats:
        from backend.format_contract import FAMILY
        from backend.runtime_catalog import validate_format_release
        release=validate_format_release(formats)
        properties=copy.deepcopy(R['Definition']['Properties'])
        properties['JobDefinitionName']=sub('openngs-pro-${TenantId}-formats')
        properties['ContainerProperties']['Image']=release['image']
        properties['Tags']['NGSRuntimeFamily']=FAMILY
        add('FormatsDefinition','AWS::Batch::JobDefinition',properties)
        outputs.update(FormatsImage={'Value':release['image']},FormatsDefinition={'Value':ref('FormatsDefinition')},
                       FormatsTools={'Value':json.dumps(release['tools'])},FormatsSource={'Value':release['source_sha256']},
                       FormatsBaseImage={'Value':release['base_image']})
    return {'AWSTemplateFormatVersion':'2010-09-09','Description':'OpenNGS funded tenant: isolated data and scale-to-zero Spot queue; no On-Demand fallback','Parameters':{'TenantId':{'Type':'String','AllowedPattern':'u_[a-f0-9]{32}'},'StorageId':{'Type':'String','AllowedPattern':'[a-f0-9]{32}'},'FleetVCPU':{'Type':'Number','AllowedValues':[2,4,8,16,32,64]},'MonthlyBudget':{'Type':'Number','MinValue':1,'MaxValue':500}},'Resources':R,'Outputs':outputs}
