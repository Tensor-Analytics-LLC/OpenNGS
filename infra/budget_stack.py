"""Scoped baseline billing alerts; no destructive billing actions or commitments."""
import json

def template(app_role):
    R={};ref=lambda n:{'Ref':n};att=lambda n,k:{'Fn::GetAtt':[n,k]};sub=lambda s:{'Fn::Sub':s}
    tags=[{'Key':'Project','Value':'OpenNGS'}]
    def add(n,t,p,**extra):R[n]={'Type':t,'Properties':p,**extra}
    add('AlertKey','AWS::KMS::Key',{'EnableKeyRotation':True,'Description':'OpenNGS budget-alert encryption only; no scientific data','KeyPolicy':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':sub('arn:aws:iam::${AWS::AccountId}:root')},'Action':'kms:*','Resource':'*'},{'Effect':'Allow','Principal':{'Service':['budgets.amazonaws.com','sns.amazonaws.com']},'Action':['kms:GenerateDataKey*','kms:Decrypt'],'Resource':'*'}]},'Tags':tags},DeletionPolicy='Retain',UpdateReplacePolicy='Retain')
    for name,suffix in [('Warning','warning'),('Critical','critical')]:
        add(name,'AWS::SNS::Topic',{'TopicName':'openngs-budget-'+suffix,'KmsMasterKeyId':ref('AlertKey'),'Tags':tags})
        add(name+'Policy','AWS::SNS::TopicPolicy',{'Topics':[ref(name)],'PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'budgets.amazonaws.com'},'Action':'sns:Publish','Resource':ref(name),'Condition':{'StringEquals':{'aws:SourceAccount':ref('AWS::AccountId')},'ArnLike':{'aws:SourceArn':sub('arn:aws:budgets::${AWS::AccountId}:budget/openngs-baseline-200')}}}]}})
    add('Inbox','AWS::SQS::Queue',{'QueueName':'openngs-budget-alerts','SqsManagedSseEnabled':True,'MessageRetentionPeriod':1209600,'VisibilityTimeout':60,'ReceiveMessageWaitTimeSeconds':20,'Tags':tags})
    add('InboxPolicy','AWS::SQS::QueuePolicy',{'Queues':[ref('Inbox')],'PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'sns.amazonaws.com'},'Action':'sqs:SendMessage','Resource':att('Inbox','Arn'),'Condition':{'ArnEquals':{'aws:SourceArn':[ref('Warning'),ref('Critical')]}}}]}})
    for topic in ['Warning','Critical']:add(topic+'Delivery','AWS::SNS::Subscription',{'TopicArn':ref(topic),'Protocol':'sqs','Endpoint':att('Inbox','Arn')})
    notifications=[]
    for threshold,kind,topic in [(50,'ACTUAL','Warning'),(75,'ACTUAL','Warning'),(90,'ACTUAL','Critical'),(100,'ACTUAL','Critical'),(90,'FORECASTED','Critical')]:
        notifications.append({'Notification':{'NotificationType':kind,'ComparisonOperator':'GREATER_THAN','Threshold':threshold,'ThresholdType':'PERCENTAGE'},'Subscribers':[{'SubscriptionType':'SNS','Address':ref(topic)}]})
    add('Baseline','AWS::Budgets::Budget',{'Budget':{'BudgetName':'openngs-baseline-200','BudgetLimit':{'Amount':200,'Unit':'USD'},'BudgetType':'COST','TimeUnit':'MONTHLY','CostFilters':{'TagKeyValue':['Project$OpenNGS','Project$ngs-studio','Project$OpenNGS-validation']},'CostTypes':{'IncludeTax':True,'IncludeCredit':False,'IncludeRefund':False,'IncludeSubscription':True,'IncludeSupport':True,'IncludeRecurring':True,'IncludeUpfront':True}},'NotificationsWithSubscribers':notifications},DependsOn=['WarningPolicy','CriticalPolicy'])
    add('ConsumerPolicy','AWS::IAM::Policy',{'PolicyName':'OpenNGSScopedBilling','Roles':[app_role],'PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['sqs:ReceiveMessage','sqs:DeleteMessage','sqs:GetQueueAttributes'],'Resource':att('Inbox','Arn')},{'Effect':'Allow','Action':['ce:GetCostAndUsage'],'Resource':'*'},{'Effect':'Allow','Action':['budgets:ViewBudget'],'Resource':sub('arn:aws:budgets::${AWS::AccountId}:budget/openngs-baseline-200')}]}})
    return {'AWSTemplateFormatVersion':'2010-09-09','Description':'OpenNGS baseline $200 actual/forecast alerts, encrypted delivery and non-destructive admission hold','Resources':R,'Outputs':{'QueueURL':{'Value':ref('Inbox')},'WarningTopic':{'Value':ref('Warning')},'CriticalTopic':{'Value':ref('Critical')},'BudgetName':{'Value':'openngs-baseline-200'}}}

if __name__=='__main__':
    import sys
    print(json.dumps(template(sys.argv[1]),indent=2))
