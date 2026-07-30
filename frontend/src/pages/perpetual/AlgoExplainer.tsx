import { Drawer, Typography } from 'antd'

const { Paragraph, Title, Text } = Typography

export default function AlgoExplainer({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Drawer title="永续组合算法详解" open={open} onClose={onClose} width={560}>
      <Typography>
        <Title level={5}>目标</Title>
        <Paragraph>
          从全库主动权益基金中，选出 10 只长期质地可靠、彼此真正低相关的基金，
          按残差风险平价配权，形成一个可以长期持有、不依赖择时的分散组合。
        </Paragraph>

        <Title level={5}>五层引擎</Title>
        <Paragraph>
          <Text strong>① 硬门槛</Text>：经理任期 ≥ 3年、规模 2~400亿、净值 ≥ 750天、
          排除持有期/定开类（名称含"年/月/季"）。
        </Paragraph>
        <Paragraph>
          <Text strong>② 质量分</Text>：滚动1年夏普中位数(35%) + 夏普稳定性(20%) +
          Sortino(25%) + 任期(10%) + 风格漂移(10%)，池内稳健 z-score 加权。
          所有分量风险调整、收益符号中立。
        </Paragraph>
        <Paragraph>
          <Text strong>③ 份额去重</Text>：同基金 A/C/E 份额只留质量最高的一只。
        </Paragraph>
        <Paragraph>
          <Text strong>④ 净值对齐</Text>：近3年窗口，覆盖度 ≥ 95%，求共同交易日。
        </Paragraph>
        <Paragraph>
          <Text strong>⑤ 分散选择 + 配权</Text>：剥离 PC1（市场beta）后在残差空间做分散。
          贪心选择综合分 = 质量 − λ×残差相关 + μ×风格覆盖。
          风险平价（ERC）配权，单基上限 20%。
        </Paragraph>

        <Title level={5}>关键设计</Title>
        <Paragraph>
          <Text strong>残差空间</Text>：原始相关 0.4~0.8 大部分是共享市场涨跌，
          剥离后残差相关才是特异性冗余。
        </Paragraph>
        <Paragraph>
          <Text strong>数据自带风格轴</Text>：PC2~PC5 是主成分坐标，非人工标签。
          两端代表基金一看便知风格含义。
        </Paragraph>
        <Paragraph>
          <Text strong>参数冻结</Text>：所有阈值一次标定即固定，不随重跑再优化，
          避免样本内过拟合。
        </Paragraph>
      </Typography>
    </Drawer>
  )
}
